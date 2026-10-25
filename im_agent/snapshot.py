"""Git release snapshots for the stdlib-only offline adapter.

Real project update receipts and non-stdlib dependency builders are separate
adapters. An arbitrary working-tree HEAD is never an adoption receipt.
"""
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import uuid

from .contracts import Rejected, canonical, fingerprint, identifier
from .ledger import Database
from .processes import background_options


class Snapshots(Database):
    def __init__(self, root, git="git"):
        self.root = Path(root)
        self.git = git
        super().__init__(self.root / "catalog.sqlite3", "host-snapshots-v1")
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS versions(id TEXT PRIMARY KEY,commit_id TEXT,manifest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS active(singleton INTEGER PRIMARY KEY CHECK(singleton=1),id TEXT,generation INTEGER);
                CREATE TABLE IF NOT EXISTS bindings(task TEXT PRIMARY KEY,snapshot TEXT NOT NULL);
            """)

    def prepare(self, repository, receipt, *, adopted_commit, lock_digest):
        if (not isinstance(receipt, dict) or set(receipt) != {"schema", "status", "source_commit", "dependency_lock_sha256", "update_id", "generation"}
                or receipt["schema"] != 1 or receipt["status"] != "succeeded"
                or not re.fullmatch(r"[a-f0-9]{40}", receipt["source_commit"])
                or receipt["source_commit"] != adopted_commit or receipt["dependency_lock_sha256"] != lock_digest
                or type(receipt["generation"]) is not int or receipt["generation"] < 1):
            raise Rejected("unverified_update_receipt")
        identifier(receipt["update_id"])
        revision = receipt["source_commit"]
        archive = subprocess.check_output([self.git, "-C", str(repository), "archive", "--format=tar", revision], timeout=30, **background_options())
        if len(archive) > 16 * 1024 * 1024:
            raise Rejected("offline_snapshot_size_limit")
        snapshot_id = fingerprint(receipt)
        staging = self.root / ("staging-" + uuid.uuid4().hex)
        staging.mkdir()
        files = {}
        try:
            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                for member in tar.getmembers():
                    name = PurePosixPath(member.name)
                    if (name.is_absolute() or ".." in name.parts or "\\" in member.name
                            or any(p in {".venv", "venv", ".env", "node_modules", ".git"} for p in name.parts)
                            or not (member.isdir() or member.isfile())):
                        raise Rejected("mutable_or_unsafe_snapshot_dependency")
                    path = staging.joinpath(*name.parts)
                    if member.isdir():
                        path.mkdir(parents=True, exist_ok=True)
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        data = tar.extractfile(member).read()
                        path.write_bytes(data)
                        files[member.name] = hashlib.sha256(data).hexdigest()
            lock = staging / "runtime-lock.json"
            if not lock.is_file() or hashlib.sha256(lock.read_bytes()).hexdigest() != lock_digest:
                raise Rejected("dependency_lock_mismatch")
            if json.loads(lock.read_text(encoding="utf-8")) != {"schema": 1, "runtime": "python-stdlib", "external_dependencies": []}:
                raise Rejected("project_dependency_builder_required")
            manifest = {"id": snapshot_id, "receipt": receipt, "files": files, "contract": "example-v1"}
            destination = self.root / snapshot_id
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                prior = db.execute("SELECT manifest FROM versions WHERE id=?", (snapshot_id,)).fetchone()
                if prior:
                    if prior[0] != canonical(manifest):
                        raise Rejected("snapshot_collision")
                    self.verify(snapshot_id)
                else:
                    os.replace(staging, destination)
                    for file in destination.rglob("*"):
                        if file.is_file():
                            file.chmod(0o444)
                    db.execute("INSERT INTO versions VALUES(?,?,?)", (snapshot_id, revision, canonical(manifest)))
            return snapshot_id
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def verify(self, snapshot_id):
        identifier(snapshot_id)
        with self.connect() as db:
            row = db.execute("SELECT manifest FROM versions WHERE id=?", (snapshot_id,)).fetchone()
        if not row:
            raise Rejected("snapshot_missing")
        manifest = json.loads(row[0])
        directory = self.root / snapshot_id
        actual = {}
        for p in directory.rglob("*"):
            if p.is_symlink():
                raise Rejected("snapshot_tampered")
            if p.is_file():
                actual[p.relative_to(directory).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
        if actual != manifest["files"]:
            raise Rejected("snapshot_tampered")
        return manifest

    def activate(self, snapshot_id):
        manifest = self.verify(snapshot_id)
        generation = manifest["receipt"]["generation"]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM active").fetchone()
            if current and generation < current["generation"]:
                raise Rejected("stale_update_rejected")
            if current and generation == current["generation"] and current["id"] != snapshot_id:
                raise Rejected("update_generation_conflict")
            db.execute("INSERT OR REPLACE INTO active VALUES(1,?,?)", (snapshot_id, generation))

    def bind(self, task):
        identifier(task)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT snapshot FROM bindings WHERE task=?", (task,)).fetchone()
            if row:
                selected = row[0]
            else:
                active = db.execute("SELECT id FROM active").fetchone()
                if not active:
                    raise Rejected("no_active_snapshot")
                selected = active[0]
                db.execute("INSERT INTO bindings VALUES(?,?)", (task, selected))
            # Verify inside the binding transaction before returning any path.
            self.verify(selected)
        return selected
