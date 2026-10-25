import importlib.util
from pathlib import Path
import tempfile
import unittest

from im_agent.contracts import Rejected

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("offline_pc", ROOT / "scripts/offline_pc.py")
offline = importlib.util.module_from_spec(spec); spec.loader.exec_module(offline)


class OfflinePackageTests(unittest.TestCase):
    def test_exact_source_commit_required_before_build(self):
        with self.assertRaisesRegex(Rejected, "fixed_commit"):
            offline.checked_repository(ROOT, "0" * 40)

    def test_dependency_filename_traversal_rejected_before_download(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); private = root / "source"; private.mkdir()
            (private / "dependencies-linux.json").write_text(json.dumps({"packages": [{"filename": "../outside.whl"}]}))
            with self.assertRaisesRegex(Rejected, "filename"):
                offline.wheelhouse(private, root / "cache")

    def test_private_package_cannot_change_dependency_host(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); private = root / "source"; private.mkdir()
            (private / "dependencies-linux.json").write_text(json.dumps({"packages": [{"filename": "package.whl", "url": "https://other.test/package.whl"}]}))
            with self.assertRaisesRegex(Rejected, "official_dependency"):
                offline.wheelhouse(private, root / "cache")
