"""Content-addressed local dependency bundles, separate from editable workspaces."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import uuid

from .contracts import Rejected, canonical, fingerprint
from .ledger import Database
from .processes import background_options


def regular(path):
    for part in [path, *path.parents]:
        try: info = part.lstat()
        except FileNotFoundError: continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise Rejected("bundle_link_or_reparse_rejected")


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024): value.update(chunk)
    return value.hexdigest()


def copy_file(source, target):
    regular(source)
    before = source.stat()
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as src, target.open("xb") as dst:
        shutil.copyfileobj(src, dst, length=1024 * 1024)
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or digest(source) != digest(target):
        raise Rejected("installed_dependency_changed_during_copy")


def copy_tree(source, target, *, exclude=()):
    regular(source)
    def files():
        for p in source.rglob("*"):
            if not any(part in {*exclude, "__pycache__"} for part in p.relative_to(source).parts): regular(p)
        return sorted(p.relative_to(source).as_posix() for p in source.rglob("*")
                      if p.is_file() and not any(part in {*exclude, "__pycache__"} for part in p.relative_to(source).parts)
                      and p.suffix not in {".pyc", ".pyo"})
    inventory = files()
    if len(inventory) > 50000: raise Rejected("dependency_file_budget")
    for name in inventory: copy_file(source / name, target / name)
    if files() != inventory: raise Rejected("installed_dependency_inventory_changed")


def export_git(git, repository, commit, target):
    if not re.fullmatch(r"[a-f0-9]{40}", commit): raise Rejected("published_commit_required")
    process = subprocess.Popen([git, "-C", str(repository), "archive", "--format=tar", commit], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **background_options())
    total, count = 0, 0
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            for member in archive:
                path = PurePosixPath(member.name)
                if (path.is_absolute() or ".." in path.parts or any(v in member.name for v in '\\:*?"<>|')
                        or not (member.isdir() or member.isfile())):
                    raise Rejected("unsafe_project_archive")
                total += member.size; count += 1
                if total > 1024**3 or count > 50000: raise Rejected("project_archive_budget")
                # A tracked placeholder is omitted entirely, including notes
                # in that file. Runtime task directories are created elsewhere.
                if path.as_posix() == "tasks" and member.isdir(): continue
                if path.as_posix() == "tasks/.gitkeep" and member.isfile(): continue
                if path.parts[0] in {"tasks", ".venv", "node_modules"} or any(v in {".env", "auth.json"} for v in path.parts):
                    raise Rejected("private_state_in_project_source")
                destination = target.joinpath(*path.parts)
                if member.isdir(): destination.mkdir(parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as src, destination.open("xb") as dst: shutil.copyfileobj(src, dst)
        if process.wait(timeout=15): raise Rejected("project_archive_failed")
    finally:
        process.stdout.close()
        if process.poll() is None: process.kill(); process.wait(timeout=5)


def inventory(root):
    result = {}
    for path in sorted(root.rglob("*")):
        regular(path)
        if path.is_file(): result[path.relative_to(root).as_posix()] = digest(path)
    return result


class Bundles(Database):
    def __init__(self, root):
        self.root = Path(root); regular(self.root)
        super().__init__(self.root / "catalog.sqlite3", "host-runtime-bundles-v1")
        with self.connect() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS versions(id TEXT PRIMARY KEY,metadata TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS active(singleton INTEGER PRIMARY KEY CHECK(singleton=1),id TEXT,generation INTEGER);
                CREATE TABLE IF NOT EXISTS bindings(task TEXT PRIMARY KEY,snapshot TEXT NOT NULL);""")

    def stage(self):
        path = self.root / ("staging-" + uuid.uuid4().hex); path.mkdir()
        return path

    def discard(self, path):
        if path.parent != self.root or not path.name.startswith("staging-"): raise Rejected("bundle_cleanup_scope_invalid")
        if path.exists(): shutil.rmtree(path)

    def seal(self, path, metadata):
        if path.parent != self.root or not path.name.startswith("staging-"): raise Rejected("bundle_staging_invalid")
        content = {"schema": 1, "metadata": metadata, "files": inventory(path)}
        identifier = fingerprint(content)
        manifest = dict(content, id=identifier)
        (path / "bundle.json").write_text(canonical(manifest), encoding="utf-8")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT metadata FROM versions WHERE id=?", (identifier,)).fetchone()
            if prior:
                self.verify(identifier); self.discard(path)
            else:
                destination = self.root / identifier
                if destination.exists(): self.verify(identifier); self.discard(path)
                else: os.replace(path, destination)
                for file in destination.rglob("*"):
                    if file.is_file(): file.chmod(0o444)
                db.execute("INSERT INTO versions VALUES(?,?)", (identifier, canonical(metadata)))
        return identifier

    def verify(self, identifier):
        if not re.fullmatch(r"[a-f0-9]{64}", identifier): raise Rejected("bundle_identifier_invalid")
        root = self.root / identifier; regular(root)
        manifest = json.loads((root / "bundle.json").read_text(encoding="utf-8"))
        if manifest.get("id") != identifier or fingerprint({k: manifest[k] for k in ["schema", "metadata", "files"]}) != identifier:
            raise Rejected("bundle_manifest_tampered")
        actual = inventory(root); actual.pop("bundle.json", None)
        if actual != manifest["files"]: raise Rejected("bundle_content_tampered")
        return manifest

    def active(self):
        with self.connect() as db:
            row = db.execute("SELECT * FROM active").fetchone()
        return dict(row) if row else None

    def activate(self, identifier, generation):
        self.verify(identifier)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM active").fetchone()
            if current and (generation < current["generation"] or generation == current["generation"] and current["id"] != identifier):
                raise Rejected("older_bundle_cannot_replace_active")
            db.execute("INSERT OR REPLACE INTO active VALUES(1,?,?)", (identifier, generation))

    def bind(self, task):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT snapshot FROM bindings WHERE task=?", (task,)).fetchone()
            active = db.execute("SELECT id FROM active").fetchone()
            if not previous and not active: raise Rejected("no_active_bundle")
            selected = previous[0] if previous else active[0]
            self.verify(selected)
            db.execute("INSERT OR IGNORE INTO bindings VALUES(?,?)", (task, selected))
        return selected
