"""A separately committed project: registered model choice, native execution, verified reply."""
import copy
import io
import json
from pathlib import Path
import shutil
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from im_agent.bundles import inventory
from im_agent.contracts import Rejected, fingerprint
from im_agent.feishu import FeishuAPI, normalize
from im_agent.feishu_fixture import Fixture
from im_agent.processes import background_options
from im_agent.service import Client, execute_once, server
from im_agent.twin import TwinTransport, TwinWorker, host, release
from im_agent.twin_config import load, validate
from im_agent.twin_project import Project
from scripts.digital_twin import checked_project, compose, export_project
from scripts.project_checks import Repository, find_git

ROOT = Path(__file__).resolve().parents[1]


class LocalProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repository = self.root / "my-project"
        shutil.copytree(ROOT / "examples/local-project/project", self.repository)
        self.git = find_git()
        import subprocess
        def git(*args):
            return subprocess.check_output([self.git, "-C", str(self.repository), *args],
                                           stderr=subprocess.DEVNULL, **background_options()).decode().strip()
        self.g = git
        git("init", "-q"); git("config", "user.name", "Synthetic"); git("config", "user.email", "synthetic@example.test")
        git("config", "commit.gpgsign", "false"); git("config", "core.autocrlf", "false")
        git("add", "."); git("commit", "-qm", "Independent synthetic project")
        self.commit = git("rev-parse", "HEAD")
        self.config = load(ROOT / "examples/local-project/instance.example.json")
        self.args = SimpleNamespace(project_repository=self.repository, project_commit=self.commit)

    def exported(self):
        repo = checked_project(self.args, self.config)
        project = self.root / "snapshot"
        metadata = export_project(repo, self.commit, project)
        return project, metadata, fingerprint(metadata["files"])

    def test_independent_model_native_project_reply_and_restart_without_replay(self):
        project, metadata, snapshot = self.exported()
        config = copy.deepcopy(self.config); config["profile"] = "feishu"
        config["feishu"]["accept_after_ms"] = 1
        config["planner"] = {"provider": "deepseek", "model": "deepseek-flash", "api_key_env": "IM_AGENT_MODEL_TEST_KEY"}
        validate(config)
        raw = json.loads((ROOT / "examples/digital-twin/message.example.json").read_text(encoding="utf-8"))
        raw["event"]["message"]["create_time"] = "2"
        question = "逐项列出检查；$(do-not-run) 只是原始文本"
        raw["event"]["message"]["content"] = json.dumps({"text": question})
        value = normalize(raw, config)
        fixture = Fixture(raw, config); self.addCleanup(fixture.close)
        api = FeishuAPI("example-app", "synthetic-secret", "example-bot", fixture_base=fixture.url)
        transport = TwinTransport(self.root / "transport", config, snapshot, api)
        worker = TwinWorker(self.root / "worker", config, snapshot)
        tokens = {"transport": "t" * 40, "executor": "e" * 40}
        http = server(("127.0.0.1", 0), worker, tokens, {"/v1/twin/ingress": ("transport", worker.intake.accept)})
        threading.Thread(target=http.serve_forever, daemon=True).start()
        self.addCleanup(http.server_close); self.addCleanup(http.shutdown)
        url = "http://127.0.0.1:" + str(http.server_port)
        client, executor = Client(url, tokens["transport"]), Client(url, tokens["executor"])
        transport.accept(value); transport.step(client)
        response = {"id": "synthetic-local-project", "model": "deepseek-flash", "choices": [{"finish_reason": "stop",
            "message": {"role": "assistant", "content": '{"capability":"list-checks"}'}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 8, "total_tokens": 58}}
        opener = Mock(); opener.open.return_value = io.BytesIO(json.dumps(response).encode())
        with patch.dict("os.environ", {"IM_AGENT_MODEL_TEST_KEY": "synthetic-model-key-never-a-real-key"}), \
                patch("urllib.request.build_opener", return_value=opener):
            worker.step()
        prompt = json.loads(opener.open.call_args.args[0].data)["messages"]
        self.assertIn("list-checks", prompt[0]["content"])
        self.assertNotIn("inspect-release", prompt[0]["content"])
        self.assertEqual(prompt[1]["content"], question)
        # Edits in the developer's original project cannot affect the accepted snapshot.
        (self.repository / "project.json").write_text("broken work in progress")
        with patch.dict("os.environ", {"IM_AGENT_TEST_APP_SECRET": "must-not-reach-child"}):
            self.assertTrue(execute_once(executor, host(self.root / "host", config, snapshot, project)))
        transport.step(client)
        answer = json.loads(next(iter(fixture.replies.values()))["body"]["content"])["text"]
        self.assertIn("文档链接：needs-review", answer)
        self.assertNotIn("版本 1.0.0", answer)  # Proves the second capability executed.
        restarted = TwinWorker(self.root / "worker", config, snapshot)
        with patch("urllib.request.build_opener", side_effect=AssertionError("no second model call")):
            restarted.intake.accept(value); restarted.step()
        resumed = TwinTransport(self.root / "transport", config, snapshot, api)
        resumed.accept(value); resumed.step(client)
        self.assertEqual(api.sends, 1); self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(restarted.status()["delivered"], 1)
        self.assertIsNone(restarted.claim("other-host"))
        self.assertEqual(restarted.status()["model_attempts"], {"verified": 1})
        self.assertEqual(metadata["commit"], self.commit)

    def test_snapshot_integrity_and_host_only_project_source(self):
        project, metadata, snapshot = self.exported()
        runtime = self.root / "runtime"; runtime.mkdir(); project.rename(runtime / "project")
        source = runtime / "source"; (source / "config").mkdir(parents=True)
        shutil.copyfile(ROOT / "config/offline-runtime.json", source / "config/offline-runtime.json")
        manifest = {"schema": 1, "framework_commit": "a" * 40, "instance": self.config,
                    "snapshot": snapshot, "source_files": inventory(source), "project": metadata}
        (runtime / "manifest.json").write_text(json.dumps(manifest))
        release(runtime, source)
        services = compose(runtime, self.config)["services"]
        for service in services.values():
            self.assertEqual(service["build"]["context"], str(source))
            self.assertFalse(any(volume["source"] == str(runtime / "project") for volume in service["volumes"]))
        (runtime / "project").rename(runtime / "host-only-project")
        release(runtime, source, verify_project=False)  # Container roles need only declarations and hashes.
        with self.assertRaisesRegex(Rejected, "snapshot_integrity"):
            release(runtime, source)
        (runtime / "host-only-project").rename(runtime / "project")
        (runtime / "project/project.json").write_text("changed")
        executor = host(runtime / "host", self.config, snapshot, runtime / "project")
        value = {"id": "om_example", "actor": self.config["actors"][0], "project": "my-local-project",
                 "capability": "inspect-status", "input": {"text": "检查"}}
        with self.assertRaisesRegex(Rejected, "snapshot_changed"):
            executor.registry.execute(value, executor.registry.binding(value))

    def test_child_environment_and_closed_output_contract(self):
        project, _, _ = self.exported()
        entrypoint = project / "im_agent_project.py"
        entrypoint.write_text('import json, os, sys\nrequest = json.load(sys.stdin)\n'
            'assert not any(k.startswith("IM_AGENT_") for k in os.environ)\n'
            'assert "PYTHONPATH" not in os.environ\n'
            'print(json.dumps({"text": request["question"]}))\n', encoding="utf-8")
        value = {"capability": "inspect-status", "input": {"text": "$(do-not-run)"}}
        def adapter():
            return Project(project, fingerprint(inventory(project)), self.config["persona"], self.config["project"])
        with patch.dict("os.environ", {"IM_AGENT_MODEL_TEST_KEY": "synthetic-secret", "PYTHONPATH": "untrusted"}):
            self.assertEqual(adapter().execute(value)["text"], "$(do-not-run)")
        for program in ['print(\'{"text":"ok","verified":true}\')', 'print("x" * 32769)',
                        'raise SystemExit(1)', 'print(\'{"text":""}\')']:
            entrypoint.write_text(program, encoding="utf-8")
            with self.subTest(program=program), self.assertRaises(Rejected): adapter().execute(value)
        with self.assertRaisesRegex(Rejected, "unregistered_project_capability"):
            adapter().execute(dict(value, capability="inspect-release"))

    def test_requires_paired_clean_full_commit_and_preserves_dirty_project(self):
        for args, config in [(SimpleNamespace(project_repository=None, project_commit=None), self.config),
                             (self.args, load(ROOT / "examples/digital-twin/instance.example.json")),
                             (SimpleNamespace(project_repository=self.repository, project_commit="main"), self.config),
                             (SimpleNamespace(project_repository=self.repository, project_commit="a" * 40), self.config)]:
            with self.subTest(args=args), self.assertRaises(Rejected): checked_project(args, config)
        (self.repository / "project.json").write_text("local unfinished edit")
        with self.assertRaisesRegex(Rejected, "dirty"):
            checked_project(self.args, self.config)
        self.assertEqual((self.repository / "project.json").read_text(), "local unfinished edit")

    def test_export_rejects_missing_entrypoint_private_state_and_symlinks(self):
        (self.repository / "im_agent_project.py").unlink()
        self.g("add", "."); self.g("commit", "-qm", "Missing entrypoint")
        repo = Repository(self.repository)
        with self.assertRaisesRegex(Rejected, "entrypoint_missing"):
            export_project(repo, self.g("rev-parse", "HEAD"), self.root / "missing")
        (self.repository / ".env").write_text("SYNTHETIC_ONLY=not-a-real-secret\n")
        self.g("add", "."); self.g("commit", "-qm", "Forbidden source state")
        with self.assertRaisesRegex(Rejected, "private_state"):
            export_project(repo, self.g("rev-parse", "HEAD"), self.root / "private")
        self.g("rm", ".env"); self.g("commit", "-qm", "Remove synthetic private state")
        # Git's mode is portable even on hosts without symlink creation privileges.
        blob = self.g("hash-object", "project.json")
        self.g("update-index", "--add", "--cacheinfo", "120000," + blob + ",link")
        self.g("commit", "-qm", "Synthetic symlink")
        with self.assertRaisesRegex(Rejected, "unsafe_project_archive"):
            export_project(repo, self.g("rev-parse", "HEAD"), self.root / "link")

    def test_submodules_and_lfs_pointers_require_a_materialized_release(self):
        for number, newline in enumerate(["\n", "\r\n"]):
            pointer = newline.join(["version https://git-lfs.github.com/spec/v1", "oid sha256:" + "a" * 64, "size 1", ""])
            (self.repository / "pointer.txt").write_bytes(pointer.encode("utf-8"))
            self.g("add", "pointer.txt"); self.g("commit", "-qm", "Synthetic LFS pointer")
            with self.subTest(newline=repr(newline)), self.assertRaisesRegex(Rejected, "lfs_requires_materialized"):
                export_project(Repository(self.repository), self.g("rev-parse", "HEAD"), self.root / ("lfs-" + str(number)))
        self.g("update-index", "--add", "--cacheinfo", "160000," + self.commit + ",nested")
        self.g("commit", "-qm", "Synthetic gitlink")
        (self.repository / "nested").mkdir()
        args = SimpleNamespace(project_repository=self.repository, project_commit=self.g("rev-parse", "HEAD"))
        with self.assertRaisesRegex(Rejected, "submodules_require_separate_release"):
            checked_project(args, self.config)

    def test_invalid_declarations_and_unregistered_model_choice_fail_closed(self):
        for items in [[], [{"name": "help", "description": "reserved"}],
                      [{"name": "inspect", "description": "x"}] * 2,
                      [{"name": "inspect", "description": "x", "command": "unregistered"}]]:
            config = copy.deepcopy(self.config); config["project"]["capabilities"] = items
            with self.subTest(items=items), self.assertRaises(Rejected): validate(config)
        _, _, snapshot = self.exported()
        worker = TwinWorker(self.root / "worker", self.config, snapshot)
        raw = json.loads((ROOT / "examples/digital-twin/message.example.json").read_text(encoding="utf-8"))
        worker.intake.accept(normalize(raw, self.config))
        worker.planner = Mock(); worker.planner.choose.return_value = "inspect-release"
        with self.assertRaisesRegex(Rejected, "planning_unknown"):
            worker.step()
        self.assertIsNone(worker.claim("host"))
        self.assertEqual(worker.intake.status(), {"unknown": 1})


if __name__ == "__main__":
    unittest.main()
