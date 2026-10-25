"""Setup authorization and credential boundaries; all identities and HTTP are synthetic."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from types import SimpleNamespace
from unittest.mock import Mock, patch

from im_agent.contracts import Rejected
from im_agent.feishu import FeishuAPI
from im_agent.feishu_fixture import Fixture
from im_agent.onboarding import Instance, Pairing, read_json, write_private
from im_agent.processes import background_options
from im_agent.twin_config import load
from scripts import init_instance as cli

CheckError = cli.CheckError

ROOT = Path(__file__).resolve().parents[1]
NOW = 1800000000000


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="im-agent-onboarding-test-")
        self.base = Path(self.temporary.name).resolve()
        self.instance = Instance.create(self.base / "instance", ROOT, "test-twin", "测试分身")

    def tearDown(self):
        self.temporary.cleanup()

    def session(self, purpose="feishu"):
        self.instance.configure(app_id="example-app", app_secret="synthetic-app-secret")
        pairing, code = self.instance.begin_pairing("example-bot", purpose=purpose, now=NOW)
        raw = read_json(ROOT / "examples/digital-twin/message.example.json")
        raw["event"]["message"].update(content=json.dumps({"text": "/pair " + code}), create_time=str(NOW + 1))
        return pairing, code, raw

    def api(self, raw):
        fixture = Fixture(raw, load(self.instance.root / "instance.json"))
        self.addCleanup(fixture.close)
        return fixture, FeishuAPI("example-app", "synthetic-app-secret", "example-bot", fixture_base=fixture.url)

    def test_new_instance_is_offline_and_has_separate_project_starter(self):
        config = load(self.instance.root / "instance.json")
        self.assertEqual(config["profile"], "offline")
        self.assertEqual(config["planner"], {"provider": "rules"})
        self.assertEqual(config["actors"][0]["instance"], "test-twin")
        self.assertTrue((self.instance.root / "project-template/im_agent_project.py").is_file())
        self.assertFalse(self.instance.status()["live_pairing_approved"])
        self.assertFalse((self.instance.root / ".git").exists())
        if os.name != "nt":
            self.assertEqual(self.instance.private.stat().st_mode & 0o777, 0o700)
            self.assertEqual((self.instance.root / "instance.json").stat().st_mode & 0o777, 0o600)

    def test_existing_source_and_nested_directories_are_preserved(self):
        before = (self.instance.root / "instance.json").read_bytes()
        for path in [self.instance.root, ROOT / "must-not-create", self.instance.private / "nested", Path.home()]:
            with self.subTest(path=path), self.assertRaises(Rejected):
                Instance.create(path, ROOT)
        self.assertEqual((self.instance.root / "instance.json").read_bytes(), before)
        self.assertFalse((ROOT / "must-not-create").exists())

    def test_symlink_parent_and_secret_file_are_never_followed(self):
        link = self.base / "linked"
        try:
            link.symlink_to(self.instance.root, target_is_directory=True)
        except OSError:
            self.skipTest("symlink creation is not available")
        with self.assertRaises(Rejected):
            Instance(link, ROOT)
        victim = self.base / "victim.json"; victim.write_text("preserve", encoding="utf-8")
        (self.instance.private / "feishu.json").symlink_to(victim)
        with self.assertRaises(Rejected):
            self.instance.configure(app_id="example-app", app_secret="synthetic-app-secret")
        self.assertEqual(victim.read_text(encoding="utf-8"), "preserve")

    def test_secrets_stay_private_and_invalid_input_does_not_change_configuration(self):
        self.instance.configure(app_id="example-app", app_secret="synthetic-app-secret", provider="deepseek",
            model="synthetic-model", model_key="synthetic-model-key")
        for path in self.instance.root.rglob("*"):
            if path.is_file() and not path.is_relative_to(self.instance.private):
                self.assertNotIn("synthetic-app-secret", path.read_text(encoding="utf-8"))
                self.assertNotIn("synthetic-model-key", path.read_text(encoding="utf-8"))
        before = self.instance.binding()
        with self.assertRaises(Rejected):
            self.instance.configure(app_id="example-app", app_secret="bad\nsecret=oops")
        self.assertEqual(self.instance.binding(), before)
        self.instance.configure(provider="rules")
        self.assertFalse((self.instance.private / "model.json").exists())
        self.assertFalse(self.instance.status()["model_connection_verified"])

    def test_secret_prompt_refuses_echo_fallback_and_noninteractive_initialization(self):
        def unavailable(prompt):
            warnings.warn("echo cannot be disabled", cli.getpass.GetPassWarning)
            self.fail("echo fallback must never be reached")
        with patch.object(cli, "terminal"), patch.object(cli.getpass, "getpass", side_effect=unavailable), self.assertRaises(Rejected):
            cli.ask_secret("Synthetic secret: ")
        target = self.base / "noninteractive-instance"
        result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/init_instance.py"), "init", "--instance-dir", str(target)],
            input=b"", capture_output=True, **background_options())
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(target.exists())
        self.assertIn(b"interactive_terminal_required", result.stderr)

    def test_private_files_are_ignored_when_instance_becomes_a_private_git_repository(self):
        self.instance.configure(app_id="example-app", app_secret="synthetic-app-secret")
        subprocess.run(["git", "-C", str(self.instance.root), "init", "-q"], check=True, **background_options())
        ignored = subprocess.run(["git", "-C", str(self.instance.root), "check-ignore", ".private/feishu.json", "instance.feishu.json", ".env"],
            capture_output=True, check=True, **background_options()).stdout.decode().splitlines()
        self.assertEqual(len(ignored), 3)
        self.assertEqual(Instance(self.instance.root, ROOT).binding(), self.instance.binding())

    def test_valid_pairing_requires_readback_and_local_confirmation_and_never_sends(self):
        pairing, code, raw = self.session()
        fixture, api = self.api(raw)
        self.assertEqual(api.bot_info(), "example-bot")
        self.assertTrue(pairing.accept(raw, api, now=NOW + 2))
        self.assertFalse(pairing.accept(raw, api, now=NOW + 3))
        self.assertFalse((self.instance.root / "instance.feishu.json").exists())
        self.assertNotIn(code, pairing.path.read_text(encoding="utf-8"))
        result = pairing.approve(True, now=NOW + 4)
        self.assertTrue(result["live_authorization_created"])
        config = self.instance.live_config()
        self.assertEqual(config["actors"][0]["user"], "example-colleague")
        self.assertEqual(config["identity"]["tenant"], "example-tenant")
        self.assertEqual(config["feishu"]["accept_after_ms"], NOW + 4)
        self.assertEqual(api.sends, 0); self.assertEqual(fixture.replies, {})
        with self.assertRaises(Rejected):
            pairing.approve(True, now=NOW + 5)

    def test_wrong_identity_code_scope_type_or_timestamp_never_becomes_candidate(self):
        pairing, _, raw = self.session()
        _, api = self.api(raw)
        variants = []
        for section, key, value in [("header", "app_id", "wrong-app"), ("header", "event_type", "wrong-event"),
            ("message", "chat_type", "group"), ("message", "message_type", "image"),
            ("message", "create_time", str(NOW - 1)), ("message", "create_time", str(NOW + 300000)),
            ("message", "create_time", "invalid"), ("sender", "sender_type", "app"),
            ("sender", "tenant_key", "wrong-tenant"), ("message", "content", json.dumps({"text": "/pair " + "0" * 32}))]:
            value_raw = copy.deepcopy(raw)
            target = value_raw["header"] if section == "header" else value_raw["event"][section]
            target[key] = value; variants.append(value_raw)
        wrong_user = copy.deepcopy(raw); wrong_user["event"]["sender"]["sender_id"]["open_id"] = "different-user"; variants.append(wrong_user)
        for item in variants:
            with self.subTest(item=item):
                self.assertFalse(pairing.accept(item, api, now=NOW + 2))
        self.assertEqual(pairing.current(NOW + 2)["status"], "waiting")

    def test_tampered_original_readback_is_not_an_authorization(self):
        pairing, _, raw = self.session()
        fixture, api = self.api(raw)
        fixture.original["body"]["content"] = json.dumps({"text": "different"})
        self.assertFalse(pairing.accept(raw, api, now=NOW + 2))
        with self.assertRaises(Rejected):
            pairing.approve(True, now=NOW + 3)

    def test_expired_pairing_and_configuration_changes_require_new_pairing(self):
        pairing, _, raw = self.session()
        _, api = self.api(raw)
        with self.assertRaises(Rejected):
            pairing.accept(raw, api, now=NOW + 300000)
        self.assertTrue(pairing.accept(raw, api, now=NOW + 2))
        self.instance.configure(provider="rules")
        with self.assertRaises(Rejected):
            pairing.approve(True, now=NOW + 3)

    def test_local_denial_and_synthetic_approval_cannot_create_live_config(self):
        for purpose, accepted in [("feishu", False), ("offline", True)]:
            pairing, _, raw = self.session(purpose)
            _, api = self.api(raw)
            self.assertTrue(pairing.accept(raw, api, now=NOW + 2))
            self.assertFalse(pairing.approve(accepted, now=NOW + 3)["live_authorization_created"])
            self.assertFalse((self.instance.root / "instance.feishu.json").exists())

    def test_approved_scope_cannot_be_broadened_without_new_pairing(self):
        pairing, _, raw = self.session()
        _, api = self.api(raw); pairing.accept(raw, api, now=NOW + 2); pairing.approve(True, now=NOW + 3)
        path = self.instance.root / "instance.feishu.json"
        config = read_json(path); config["actors"][0]["user"] = "different-user"; write_private(path, config)
        with self.assertRaises(Rejected):
            self.instance.live_config()
        self.assertFalse(self.instance.status()["live_pairing_approved"])

    def test_bound_runtime_blocks_reconfigure_pairing_and_second_preparation(self):
        write_private(self.instance.private / "live-runtime.json", {"status": "preparing", "runtime": "synthetic"})
        for action in [lambda: self.instance.configure(provider="rules"),
                       lambda: self.instance.begin_pairing("example-bot", purpose="offline"),
                       lambda: cli.prepare_live(self.instance, SimpleNamespace())]:
            with self.assertRaises(Rejected):
                action()
        self.assertEqual(read_json(self.instance.private / "live-runtime.json")["status"], "preparing")

    def test_failed_live_preparation_keeps_binding_instead_of_creating_a_new_ledger(self):
        pairing, _, raw = self.session()
        _, api = self.api(raw); pairing.accept(raw, api, now=NOW + 2); pairing.approve(True, now=NOW + 3)
        args = SimpleNamespace(runtime=self.base / "runtime", commit="a" * 40)
        with patch.object(cli, "prepare", side_effect=Rejected("synthetic_prepare_failure")), self.assertRaises(Rejected):
            cli.prepare_live(self.instance, args)
        self.assertEqual(read_json(self.instance.private / "live-runtime.json")["status"], "preparing")
        with self.assertRaises(Rejected):
            cli.prepare_live(self.instance, args)

    def test_selected_project_requires_clean_git_snapshot_and_does_not_execute_code(self):
        project = self.base / "project"
        shutil.copytree(ROOT / "examples/local-project/project", project)
        (project / "im_agent_project.py").write_text("raise RuntimeError('must not execute during selection')\n", encoding="utf-8")
        def git(*args):
            return subprocess.check_output(["git", "-C", str(project), *args], **background_options()).decode().strip()
        git("init", "-q"); git("config", "user.name", "Synthetic"); git("config", "user.email", "synthetic@example.test")
        git("config", "commit.gpgsign", "false"); git("config", "core.autocrlf", "false")
        git("add", "."); git("commit", "-qm", "Synthetic project")
        definition = self.instance.root / "project-template/capabilities.json"
        selected = cli.selection(self.instance, project, definition)
        self.instance.configure(project=selected)
        self.assertEqual(self.instance.settings()["project_source"]["commit"], git("rev-parse", "HEAD"))
        (project / "project.json").write_text("changed", encoding="utf-8")
        with self.assertRaises((Rejected, CheckError)):
            cli.selection(self.instance, project, definition)

    def test_pairing_child_is_stopped_if_the_pairing_session_expires(self):
        self.instance.configure(app_id="example-app", app_secret="synthetic-app-secret")
        child = Mock(); child.poll.return_value = None
        with patch.object(cli, "terminal"), patch.object(cli.importlib.util, "find_spec", return_value=True), \
                patch.object(cli.FeishuAPI, "bot_info", return_value="example-bot"), \
                patch.object(Pairing, "current", side_effect=[{"id": "synthetic-child"}, Rejected("pairing_expired_or_configuration_changed")]), \
                patch.object(cli.subprocess, "Popen", return_value=child), patch("builtins.print"), self.assertRaises(Rejected):
            cli.pair(self.instance, SimpleNamespace(connect=True, timeout=30))
        child.terminate.assert_called_once_with(); child.wait.assert_called_once_with(timeout=5)


if __name__ == "__main__":
    unittest.main()
