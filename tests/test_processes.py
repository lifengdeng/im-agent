import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from im_agent.processes import background_options


class BackgroundProcessTests(unittest.TestCase):
    def test_desktop_observer_bootstraps_in_isolated_mode(self):
        root = Path(__file__).resolve().parents[1]
        child = subprocess.run([sys.executable, "-I", "-B", str(root / "scripts/check_background_windows.py"), "--help"],
                               capture_output=True, text=True, timeout=10, **background_options())
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertIn("--runtime-root", child.stdout)

    def test_captured_input_output_and_exit_status_survive(self):
        child = subprocess.run([sys.executable, "-B", "-c", "import sys; print(sys.stdin.read()); sys.exit(7)"],
                               input="synthetic", capture_output=True, text=True, timeout=10, **background_options())
        self.assertEqual(child.stdout.strip(), "synthetic")
        self.assertEqual(child.returncode, 7)

    @unittest.skipUnless(os.name == "nt", "requires real Windows console allocation")
    def test_windowless_parent_does_not_allocate_child_consoles(self):
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        self.assertTrue(pythonw.is_file())
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "report.json"
            root = Path(__file__).resolve().parents[1]
            parent = subprocess.Popen([str(pythonw), "-I", "-B", str(root / "tests/fixtures/background_parent.py"),
                                       str(root), str(report), sys.executable], **background_options())
            self.addCleanup(lambda: parent.poll() is None and parent.kill())
            self.assertEqual(parent.wait(timeout=40), 0)
            value = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(value["parent_console"], 0)
            self.assertEqual([v["console"] for v in value["children"]], [0, 0, 0])
            self.assertEqual(value["children"][0]["echo"], "pipe-roundtrip")
