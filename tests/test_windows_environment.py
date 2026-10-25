"""Offline checks for credential isolation and safe installation boundaries."""
from __future__ import annotations

import io
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import windows_environment as env


class IsolationTests(unittest.TestCase):
    def test_docker_status_cannot_follow_inherited_remote_context(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "Programs/DockerDesktop/resources/bin/docker.exe"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"fixture")
            variables = {"LOCALAPPDATA": str(root), "ProgramFiles": str(root), "DOCKER_HOST": "tcp://example.invalid", "DOCKER_CONTEXT": "remote"}
            response = SimpleNamespace(returncode=0, stdout='{"Version":"test", "Os":"linux", "Arch":"amd64"}')
            with patch.dict(os.environ, variables), patch.object(env, "command", return_value=response) as execute:
                result = env.docker_status()
            self.assertTrue(result["engine_ready"])
            self.assertIn("npipe:////./pipe/dockerDesktopLinuxEngine", execute.call_args.args[0])
            self.assertNotIn("DOCKER_CONTEXT", execute.call_args.kwargs["env"])
            self.assertNotIn("DOCKER_HOST", execute.call_args.kwargs["env"])

    def test_npm_platform_keeps_full_native_tree_and_distribution_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            vendor = stage / "package/vendor" / env.LOCK["target"]
            (vendor / "bin").mkdir(parents=True)
            (vendor / "bin/codex.exe").write_bytes(b"fixture")
            (stage / "package/package.json").write_text('{}', encoding="utf-8")
            self.assertEqual(env.package_tree(stage), vendor)
            self.assertTrue((vendor / "bin/codex.exe").is_file())
            self.assertEqual((vendor / "npm-distribution/package.json").read_text(), '{}')

    def test_parallel_download_assembles_only_validated_ranges(self):
        payload = bytes(range(200))
        def response(request, **kwargs):
            start, end = map(int, request.headers["Range"].removeprefix("bytes=").split("-"))
            stream = io.BytesIO(payload[start:end + 1])
            stream.status = 206
            stream.headers = {"Content-Range": f"bytes {start}-{end}/{len(payload)}"}
            return stream
        with tempfile.TemporaryDirectory() as temporary, patch.object(env.urllib.request, "urlopen", side_effect=response):
            target = Path(temporary) / "download.part"
            env.download_parts("https://example.invalid/package", len(payload), target)
            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_server_ignoring_range_is_rejected(self):
        def response(*args, **kwargs):
            stream = io.BytesIO(b"wrong")
            stream.status = 200
            stream.headers = {}
            return stream
        with tempfile.TemporaryDirectory() as temporary, patch.object(env.urllib.request, "urlopen", side_effect=response):
            with self.assertRaisesRegex(env.PreparationError, "range_response_invalid"):
                env.download_parts("https://example.invalid/package", 100, Path(temporary) / "download.part")

    def test_inherited_auth_cannot_select_personal_credentials(self):
        original = {name: "synthetic" for name in env.AUTH_ENV}
        original.update(CODEX_HOME="personal", PATH="kept")
        isolated = env.codex_env(Path("instance"), original)
        self.assertEqual(isolated["CODEX_HOME"], str(Path("instance/codex-home")))
        self.assertEqual(isolated["PATH"], "kept")
        self.assertFalse(any(name in isolated for name in env.AUTH_ENV))
        self.assertEqual(original["CODEX_HOME"], "personal")

    def test_source_and_personal_home_are_rejected(self):
        for path in (env.ROOT, env.ROOT / ".local/runtime", Path.home() / ".codex/agent"):
            with self.subTest(path=path), self.assertRaises(env.PreparationError):
                env.runtime_path(str(path))

    def test_existing_configuration_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text("personal", encoding="utf-8")
            with self.assertRaises(env.PreparationError):
                env.write_new_or_same(path, env.CONFIG)
            self.assertEqual(path.read_text(encoding="utf-8"), "personal")

    def test_archive_rejects_traversal_and_links_before_writing(self):
        for name, kind in (("../escape", tarfile.REGTYPE), ("safe/link", tarfile.SYMTYPE), ("C:/escape", tarfile.REGTYPE), ("safe\\escape", tarfile.REGTYPE)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                archive = root / "package.tar.gz"
                target = root / "candidate"
                target.mkdir()
                with tarfile.open(archive, "w:gz") as tar:
                    info = tarfile.TarInfo(name)
                    info.type = kind
                    info.linkname = "outside" if kind == tarfile.SYMTYPE else ""
                    tar.addfile(info, io.BytesIO())
                with self.assertRaises(env.PreparationError):
                    env.extract(archive, target)
                self.assertEqual(list(target.iterdir()), [])

    def test_symlink_root_does_not_escape_isolation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.mkdir()
            link = root / "link"
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError:
                self.skipTest("symlink creation unavailable")
            with self.assertRaises(env.PreparationError):
                env.runtime_path(str(link))


if __name__ == "__main__":
    unittest.main()
