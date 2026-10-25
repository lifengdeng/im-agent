import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

from im_agent.contracts import Rejected
from im_agent.snapshot import Snapshots
from scripts.project_checks import find_git


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.cleanup)
        self.root = Path(self.tmp.name); self.repo = self.root / "developer"; self.repo.mkdir()
        self.git = find_git()
        self.g("init", "-q")
        self.g("config", "user.name", "Synthetic")
        self.g("config", "user.email", "synthetic@example.test")
        self.g("config", "commit.gpgsign", "false")
        self.g("config", "core.autocrlf", "false")
        lock = b'{"schema":1,"runtime":"python-stdlib","external_dependencies":[]}\n'
        (self.repo / "runtime-lock.json").write_bytes(lock)
        (self.repo / "工程说明.txt").write_text("发行版包含中文文件名。\n", encoding="utf-8")
        (self.repo / "main.py").write_text("VERSION = 'A'\n")
        self.g("add", "."); self.g("commit", "-qm", "Synthetic A")
        self.lock_digest = hashlib.sha256(lock).hexdigest()
        self.a = self.g("rev-parse", "HEAD")
        self.snapshots = Snapshots(self.root / "snapshots", git=self.git)

    def cleanup(self):
        for path in Path(self.tmp.name).rglob("*"):
            if path.is_file(): path.chmod(stat.S_IREAD | stat.S_IWRITE)
        self.tmp.cleanup()

    def g(self, *args):
        return subprocess.check_output([self.git, "-C", str(self.repo), *args], stderr=subprocess.DEVNULL).decode().strip()

    def receipt(self, commit, generation):
        return {"schema": 1, "status": "succeeded", "source_commit": commit,
                "dependency_lock_sha256": self.lock_digest, "update_id": "synthetic-" + str(generation), "generation": generation}

    def prepare(self, commit, generation):
        return self.snapshots.prepare(self.repo, self.receipt(commit, generation), adopted_commit=commit, lock_digest=self.lock_digest)

    def test_dirty_developer_changes_do_not_stop_old_or_pollute_new_snapshot(self):
        first = self.prepare(self.a, 1); self.snapshots.activate(first)
        self.assertEqual((self.snapshots.root / first / "工程说明.txt").read_text(encoding="utf-8"), "发行版包含中文文件名。\n")
        self.assertEqual(self.snapshots.bind("old-task"), first)
        (self.repo / "main.py").write_text("VERSION = 'B'\n")
        self.g("add", "."); self.g("commit", "-qm", "Synthetic B")
        b = self.g("rev-parse", "HEAD")
        (self.repo / "main.py").write_text("uncommitted broken development\n")
        second = self.prepare(b, 2); self.snapshots.activate(second)
        self.assertEqual(self.snapshots.bind("old-task"), first)
        self.assertEqual(self.snapshots.bind("new-task"), second)
        self.assertEqual((self.snapshots.root / second / "main.py").read_text(), "VERSION = 'B'\n")
        self.assertEqual((self.repo / "main.py").read_text(), "uncommitted broken development\n")
        with self.assertRaisesRegex(Rejected, "stale_update"):
            self.snapshots.activate(first)

    def test_failed_update_keeps_active_and_adopted_commit_cannot_be_substituted(self):
        first = self.prepare(self.a, 1); self.snapshots.activate(first)
        receipt = dict(self.receipt(self.a, 2), status="failed")
        with self.assertRaises(Rejected): self.snapshots.prepare(self.repo, receipt, adopted_commit=self.a, lock_digest=self.lock_digest)
        with self.assertRaises(Rejected): self.snapshots.prepare(self.repo, self.receipt(self.a, 2), adopted_commit="0" * 40, lock_digest=self.lock_digest)
        self.assertEqual(self.snapshots.bind("still-serving"), first)

    def test_dependency_lock_and_snapshot_tamper_rejected(self):
        first = self.prepare(self.a, 1); self.snapshots.activate(first)
        path = self.snapshots.root / first / "main.py"; path.chmod(0o644); path.write_text("tampered")
        with self.assertRaisesRegex(Rejected, "tampered"): self.snapshots.bind("new-task")
        receipt = dict(self.receipt(self.a, 2), dependency_lock_sha256="0" * 64)
        with self.assertRaises(Rejected): self.snapshots.prepare(self.repo, receipt, adopted_commit=self.a, lock_digest="0" * 64)
