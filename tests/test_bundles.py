import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest

from im_agent.bundles import Bundles, copy_tree, export_git
from im_agent.contracts import Rejected


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.cleanup)
        self.root = Path(self.tmp.name).resolve(); self.store = Bundles(self.root / "bundles")

    def cleanup(self):
        for p in self.root.rglob("*"):
            if p.is_file() and not p.is_symlink(): p.chmod(stat.S_IREAD | stat.S_IWRITE)
        self.tmp.cleanup()

    def bundle(self, value):
        path = self.store.stage(); (path / "entry.py").write_text(value, encoding="utf-8")
        return self.store.seal(path, {"release": value})

    def test_existing_task_keeps_previous_source_and_dependencies(self):
        a = self.bundle("A"); self.store.activate(a, 1)
        self.assertEqual(self.store.bind("old-task"), a)
        b = self.bundle("B"); self.store.activate(b, 2)
        self.assertEqual(self.store.bind("old-task"), a)
        self.assertEqual(self.store.bind("new-task"), b)
        with self.assertRaises(Rejected): self.store.activate(a, 1)

    def test_dependency_bytes_cannot_be_changed_after_sealing(self):
        a = self.bundle("A"); path = self.store.root / a / "entry.py"
        path.chmod(0o644); path.write_text("edited")
        with self.assertRaisesRegex(Rejected, "tampered"): self.store.verify(a)

    def test_copy_tree_does_not_link_back_to_developer_dependency(self):
        source = self.root / "developer"; source.mkdir(); (source / "module.py").write_text("A")
        target = self.root / "copy"; copy_tree(source, target)
        (source / "module.py").write_text("B")
        self.assertEqual((target / "module.py").read_text(), "A")

    @unittest.skipIf(os.name == "nt", "POSIX symlink fixture; Windows uses a junction fixture")
    def test_external_dependency_symlink_rejected(self):
        source = self.root / "developer"; source.mkdir()
        (source / "external").symlink_to(self.root / "bundles", target_is_directory=True)
        with self.assertRaisesRegex(Rejected, "link_or_reparse"): copy_tree(source, self.root / "copy")

    @unittest.skipUnless(os.name == "nt", "Windows directory junction fixture")
    def test_windows_dependency_junction_is_rejected(self):
        source = self.root / "developer"; source.mkdir(); junction = source / "external"
        subprocess.run([os.environ["COMSPEC"], "/d", "/c", "mklink", "/J", str(junction), str(self.store.root)],
                       capture_output=True, timeout=10, check=True)
        try:
            with self.assertRaisesRegex(Rejected, "link_or_reparse"): copy_tree(source, self.root / "copy")
        finally: os.rmdir(junction)

    def test_cleanup_cannot_delete_accepted_bundle(self):
        a = self.bundle("A")
        with self.assertRaises(Rejected): self.store.discard(self.store.root / a)

    def test_tracked_task_marker_is_omitted_but_task_data_is_rejected(self):
        git = str(Path(os.environ["ProgramFiles"]) / "Git/cmd/git.exe") if os.name == "nt" else "git"
        if not Path(git).is_file() and not shutil.which(git): self.skipTest("Synthetic archive fixture requires native Git")
        project = self.root / "source"; project.mkdir(); (project / "tasks").mkdir()
        def g(*args):
            return subprocess.check_output([git, "-C", str(project), *args], stderr=subprocess.DEVNULL).decode().strip()
        g("init", "-q"); g("config", "user.name", "Synthetic"); g("config", "user.email", "synthetic@example.test")
        g("config", "commit.gpgsign", "false"); g("config", "core.autocrlf", "false")
        (project / "tasks/.gitkeep").write_bytes(b"Synthetic marker note\n"); (project / "entry.py").write_text("fixture")
        g("add", "."); g("commit", "-qm", "Synthetic source")
        target = self.root / "export"; export_git(git, project, g("rev-parse", "HEAD"), target)
        self.assertFalse((target / "tasks").exists()); self.assertTrue((target / "entry.py").is_file())
        (project / "tasks/private.json").write_text("synthetic task")
        g("add", "."); g("commit", "-qm", "Synthetic invalid task data")
        with self.assertRaisesRegex(Rejected, "private_state"):
            export_git(git, project, g("rev-parse", "HEAD"), self.root / "rejected-export")
