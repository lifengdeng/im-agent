"""Behavior tests with isolated Git repositories and synthetic data only."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from project_checks import Repository, blocked_path, check_repository, find_git, inspect_content


class PathPolicyTests(unittest.TestCase):
    def test_private_and_runtime_files_are_rejected(self):
        for name in (".env", ".env.production", "secrets/model.json", "Secrets/model.json", "STATE/report.json", "state/queue.sqlite3", ".local/report.json", "auth.json", "cache/queue.db-wal", ".venv/bin/python"):
            with self.subTest(name=name):
                self.assertTrue(blocked_path(name))

    def test_code_and_placeholder_templates_are_allowed(self):
        for name in ("src/im_agent/memory/store.py", "docs/记忆设计.md", ".env.example", "config/env.example"):
            with self.subTest(name=name):
                self.assertFalse(blocked_path(name))

    def test_secret_detection_does_not_echo_secret(self):
        synthetic = "ghp_" + "A" * 36
        issues = inspect_content("example.txt", "100644", synthetic.encode())
        self.assertEqual([i.code for i in issues], ["credential_pattern_detected"])
        self.assertNotIn(synthetic, repr(issues))

    def test_private_key_and_assignment_detection(self):
        marker = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
        self.assertEqual(inspect_content("sample.txt", "100644", marker.encode())[0].code, "credential_pattern_detected")
        name = "FEISHU_APP_" + "SECRET"
        issues = inspect_content("sample.txt", "100644", (name + "=" + "x" * 24).encode())
        self.assertEqual(issues[0].code, "credential_assignment_detected")
        self.assertEqual(inspect_content(".env.example", "100644", (name + "=REPLACE_ME").encode()), [])

    def test_syntax_is_checked_without_running_code(self):
        harmless = b"raise RuntimeError('must never execute')\n"
        self.assertEqual(inspect_content("sample.py", "100644", harmless), [])
        self.assertEqual(inspect_content("sample.py", "100644", b"def broken(\n")[0].code, "python_syntax_invalid")

    def test_symbolic_links_are_rejected_without_dereference(self):
        self.assertEqual(inspect_content("link.py", "120000", b"/private/path")[0].code, "symlink_or_embedded_repository_not_supported")


class GitStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git = find_git()
        self.run_git("init", "--initial-branch=main")
        self.run_git("config", "user.name", "Fixture")
        self.run_git("config", "user.email", "fixture@example.invalid")
        self.repo = Repository(self.root, self.git)

    def run_git(self, *args):
        result = subprocess.run([self.git, "-C", str(self.root), *args], capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, "fixture git operation failed: " + args[0])
        return result

    def test_staged_scan_reads_index_not_repaired_worktree(self):
        file = self.root / "module.py"
        file.write_text("def broken(\n", encoding="utf-8")
        self.run_git("add", "module.py")
        file.write_text("value = 1\n", encoding="utf-8")
        result = check_repository(self.repo, "staged")
        self.assertFalse(result["passed"])
        self.assertIn("python_syntax_invalid", [i["code"] for i in result["issues"]])

    def test_forced_runtime_file_is_blocked(self):
        (self.root / ".env").write_text("PLACEHOLDER=demo\n", encoding="utf-8")
        self.run_git("add", ".env")
        self.assertIn("private_or_generated_path", [i["code"] for i in check_repository(self.repo, "staged")["issues"]])

    def test_hidden_tracked_flags_are_blocked(self):
        (self.root / "module.py").write_text("value = 1\n", encoding="utf-8")
        self.run_git("add", "module.py")
        for flag in ("--assume-unchanged", "--skip-worktree"):
            with self.subTest(flag=flag):
                self.run_git("update-index", flag, "module.py")
                self.assertIn("hidden_tracked_file", [i["code"] for i in check_repository(self.repo, "staged")["issues"]])
                self.run_git("update-index", "--no-assume-unchanged", "--no-skip-worktree", "module.py")

    def test_head_scan_is_not_changed_by_dirty_worktree(self):
        file = self.root / "module.py"
        file.write_text("value = 1\n", encoding="utf-8")
        self.run_git("add", "module.py")
        self.run_git("commit", "-m", "fixture")
        file.write_text("def broken(\n", encoding="utf-8")
        self.assertTrue(check_repository(self.repo, "head")["passed"])
        self.assertFalse(check_repository(self.repo, "worktree")["passed"])

    def test_existing_other_hooks_path_is_preserved(self):
        spec = importlib.util.spec_from_file_location("agent_bootstrap", SCRIPTS / "bootstrap.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.run_git("config", "core.hooksPath", "custom-hooks")
        with self.assertRaisesRegex(RuntimeError, "existing_hooks_path_preserved"):
            module.install_hooks(self.repo)
        self.assertEqual(self.repo.value("core.hooksPath"), "custom-hooks")


if __name__ == "__main__":
    unittest.main()
