import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from resident_pc import check_release
import resident_pc
import windows_environment
import resident_verify
from im_agent.bundles import inventory
from im_agent.contracts import Rejected


class ReleaseTests(unittest.TestCase):
    def test_private_verifier_cannot_escape_fixed_instance_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            release = Path(tmp).resolve(); instance = release / "instance"; instance.mkdir()
            declaration = instance / "instance-release.json"
            args = SimpleNamespace(runtime_root=release)
            with patch.object(resident_pc, "paths", return_value=(release, release)), patch.object(resident_pc, "selected", return_value="fixed"), patch.object(resident_pc, "check_release", return_value=(release, {})):
                for name in ["../outside.py", "/absolute.py", "C:/external.py"]:
                    declaration.write_text(json.dumps({"offline_resident_verifier": name}))
                    with self.assertRaises(Rejected): resident_verify.verify(args)
                declaration.write_text(json.dumps({"offline_resident_verifier": "check.py"}))
                (instance / "check.py").write_text('def verify(args): return {"fixture": True}\n')
                self.assertEqual(resident_verify.verify(args), {"fixture": True})

    def test_release_verifies_source_and_rejects_mutation_or_path_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); identifier = "a" * 24
            directory = root / "releases" / identifier; directory.mkdir(parents=True)
            (directory / "module.py").write_text("synthetic")
            manifest = {"id": identifier, "schema": 3, "production_enabled": False, "files": inventory(directory)}
            (directory / "release.json").write_text(json.dumps(manifest))
            self.assertEqual(check_release(root, identifier)[1], manifest)
            with self.assertRaises(Rejected): check_release(root, "../outside")
            (directory / "module.py").write_text("changed")
            with self.assertRaises(Rejected): check_release(root, identifier)

    def test_prepared_runtime_accepts_only_immutable_release_runner_inside_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp).resolve() / "runtime"; runtime.mkdir()
            (runtime / "instance.json").write_text(json.dumps({"schema": 1, "profile": "isolated-codex"}))
            source = runtime / "resident/releases" / ("a" * 24) / "framework"
            with patch.object(resident_pc, "ROOT", source), patch.object(windows_environment, "ROOT", source):
                self.assertEqual(resident_pc.paths(SimpleNamespace(runtime_root=runtime))[0], runtime)
            with patch.object(resident_pc, "ROOT", runtime / "editable"), patch.object(windows_environment, "ROOT", runtime / "editable"):
                with self.assertRaises(windows_environment.PreparationError): resident_pc.paths(SimpleNamespace(runtime_root=runtime))
