"""Public sample behavior with synthetic users, HTTP fixtures and a real local child."""
import copy
import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

from im_agent.bundles import inventory
from im_agent.contracts import Rejected, fingerprint
from im_agent.feishu import FeishuAPI, normalize
from im_agent.feishu_fixture import Fixture
from im_agent.intake import Inbox
from im_agent.ledger import Worker, Transport
from im_agent.service import Client, execute_once, server
from im_agent.twin import TwinTransport, TwinWorker, host
from im_agent.twin_config import load, registry, validate
from im_agent.twin_planner import Planner

ROOT = Path(__file__).resolve().parents[1]


class TwinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.config = load(ROOT / "examples/digital-twin/instance.example.json")
        self.raw = json.loads((ROOT / "examples/digital-twin/message.example.json").read_text(encoding="utf-8"))
        self.project = self.root / "project"
        shutil.copytree(ROOT / "examples/digital-twin/project", self.project)
        self.snapshot = fingerprint(inventory(self.project))
        self.value = normalize(self.raw, self.config)

    def service(self, planner=None):
        worker = TwinWorker(self.root / "worker", self.config, self.snapshot, planner)
        tokens = {"transport": "t" * 40, "executor": "e" * 40}
        http = server(("127.0.0.1", 0), worker, tokens, {"/v1/twin/ingress": ("transport", worker.intake.accept)})
        threading.Thread(target=http.serve_forever, daemon=True).start()
        self.addCleanup(http.server_close); self.addCleanup(http.shutdown)
        url = "http://127.0.0.1:" + str(http.server_port)
        return worker, Client(url, tokens["transport"]), Client(url, tokens["executor"])

    def test_http_intake_native_project_readback_and_duplicate_after_restart(self):
        fixture = Fixture(self.raw, self.config); self.addCleanup(fixture.close)
        api = FeishuAPI("example-app", "synthetic-secret", "example-bot", fixture_base=fixture.url)
        transport = TwinTransport(self.root / "transport", self.config, self.snapshot, api)
        worker, client, executor = self.service()
        transport.accept(self.value)
        self.assertTrue(transport.accept(self.value)["duplicate"])
        transport.step(client); worker.step()
        self.assertTrue(execute_once(executor, host(self.root / "host", self.config, self.snapshot, self.project)))
        transport.step(client)
        self.assertEqual(worker.status()["delivered"], 1)
        answer = json.loads(next(iter(fixture.replies.values()))["body"]["content"])["text"]
        self.assertIn("10% → 100%", answer)
        self.assertIn("需要复核", answer)
        restarted = TwinTransport(self.root / "transport", self.config, self.snapshot, api)
        self.assertTrue(restarted.accept(self.value)["duplicate"])
        restarted.step(client)
        self.assertEqual(api.sends, 1)
        self.assertEqual(len(fixture.replies), 1)
        restarted_worker = TwinWorker(self.root / "worker", self.config, self.snapshot)
        self.assertEqual(restarted_worker.status()["delivered"], 1)
        self.assertIsNone(restarted_worker.claim("other-host"))

    def test_lost_worker_receipt_ack_reuses_verified_transport_receipt(self):
        fixture = Fixture(self.raw, self.config); self.addCleanup(fixture.close)
        api = FeishuAPI("example-app", "synthetic-secret", "example-bot", fixture_base=fixture.url)
        transport = TwinTransport(self.root / "transport", self.config, self.snapshot, api)
        worker, client, executor = self.service()
        transport.accept(self.value); transport.step(client); worker.step()
        execute_once(executor, host(self.root / "host", self.config, self.snapshot, self.project))
        original = client.call
        def lost(path, value):
            if path == "/v1/receipt":
                raise TimeoutError("synthetic lost ack")
            return original(path, value)
        with patch.object(client, "call", side_effect=lost), self.assertRaises(TimeoutError):
            transport.step(client)
        transport.step(client)
        self.assertEqual(api.sends, 1)
        self.assertEqual(worker.status()["delivered"], 1)

    def test_unverified_send_becomes_unknown_and_is_never_resent(self):
        transport = Transport(self.root / "delivery", receipt_kind="feishu")
        calls = []
        def uncertain(value):
            calls.append(value)
            raise TimeoutError("accepted but readback lost")
        intent = {"id": "om_question", "result": {"text": "synthetic"}}
        with self.assertRaises(TimeoutError):
            transport.deliver(intent, uncertain)
        restarted = Transport(transport.path, receipt_kind="feishu")
        with self.assertRaisesRegex(Rejected, "unknown_delivery"):
            restarted.deliver(intent, uncertain)
        self.assertEqual(len(calls), 1)

    def test_cross_chat_readback_fails_before_a_reply(self):
        api = FeishuAPI("example-app", "synthetic-secret", "example-bot")
        with patch.object(api, "message", return_value={"chat_id": "other", "sender": {"id": "example-colleague", "sender_type": "user"}}):
            with self.assertRaisesRegex(Rejected, "anchor_rejected"):
                api.reply({"id": "om_question", "actor": self.value["actor"]})
        self.assertEqual(api.sends, 0)

    def test_wrong_reply_content_is_unknown_instead_of_success(self):
        api = FeishuAPI("example-app", "synthetic-secret", "example-bot")
        intent = {"id": "om_question", "actor": self.value["actor"], "result": {"text": "verified answer", "evidence": self.snapshot}}
        original = {"chat_id": "example-private-chat", "sender": {"id": "example-colleague", "sender_type": "user"}}
        changed = {"chat_id": "example-private-chat", "msg_type": "text", "sender": {"id": "example-app", "sender_type": "app"}, "body": {"content": '{"text":"changed"}'}}
        with patch.object(api, "message", side_effect=[original, changed]), patch.object(api, "request", return_value={"data": {"message_id": "om_reply"}}):
            with self.assertRaisesRegex(Rejected, "unverified"):
                api.reply(intent)

    def test_sender_tenant_app_and_owner_are_not_interchangeable(self):
        for target, key, value in [("header", "app_id", "other-app"), ("header", "tenant_key", "other-tenant")]:
            raw = copy.deepcopy(self.raw); raw[target][key] = value
            with self.subTest(key=key), self.assertRaises(Rejected):
                normalize(raw, self.config)
        raw = copy.deepcopy(self.raw); raw["event"]["sender"]["sender_id"]["open_id"] = self.config["owner"]
        with self.assertRaises(Rejected):
            normalize(raw, self.config)
        bad = dict(self.value, actor=dict(self.value["actor"], chat="other-room"))
        intake = Inbox(self.root / "intake", registry(self.config, self.snapshot, intake=True))
        with self.assertRaises(Rejected):
            intake.accept(bad)

    def test_group_requires_allowlisted_sender_and_explicit_bot_mention(self):
        config = copy.deepcopy(self.config)
        config["actors"][0]["kind"] = "group"
        raw = copy.deepcopy(self.raw); raw["event"]["message"]["chat_type"] = "group"
        with self.assertRaises(Rejected):
            normalize(raw, config)
        raw["event"]["message"]["mentions"] = [{"key": "@_user_1", "id": {"open_id": "example-bot"}}]
        raw["event"]["message"]["content"] = json.dumps({"text": "@_user_1 检查发布"})
        self.assertEqual(normalize(raw, config)["input"]["text"], "检查发布")

    def test_live_instance_requires_persisted_cutoff_and_rejects_historical_events(self):
        config = copy.deepcopy(self.config); config["profile"] = "feishu"
        with self.assertRaisesRegex(Rejected, "cutoff_required"):
            validate(config)
        config["feishu"]["accept_after_ms"] = 1800000000000
        validate(config)
        for created in [None, "1799999999999", 1800000000000, "invalid"]:
            raw = copy.deepcopy(self.raw); raw["event"]["message"]["create_time"] = created
            with self.subTest(created=created), self.assertRaises(Rejected):
                normalize(raw, config)
        raw["event"]["message"]["create_time"] = "1800000000000"
        self.assertEqual(normalize(raw, config), self.value)
        # The cutoff is instance data, never a new timestamp on reconnection.
        saved = self.root / "instance.json"; saved.write_text(json.dumps(config))
        self.assertEqual(normalize(raw, load(saved)), self.value)
        for cutoff in [True, 0, -1, "1800000000000", 10**14]:
            config["feishu"]["accept_after_ms"] = cutoff
            with self.subTest(cutoff=cutoff), self.assertRaises(Rejected):
                validate(config)

    def test_unknown_model_call_survives_restart_without_replay(self):
        class Unknown:
            calls = 0
            def choose(self, text):
                self.calls += 1
                raise TimeoutError("synthetic unknown model")
        planner = Unknown()
        intake = Inbox(self.root / "intake", registry(self.config, self.snapshot, intake=True), recover_planning=True)
        intake.accept(self.value)
        with self.assertRaisesRegex(Rejected, "unknown_no_replay"):
            intake.plan(planner, self.fail)
        restarted = Inbox(intake.path, intake.registry, recover_planning=True)
        restarted.accept(self.value); restarted.plan(planner, self.fail)
        self.assertEqual(planner.calls, 1)
        self.assertEqual(restarted.status(), {"unknown": 1})

    def test_persisted_plan_retries_ingress_without_another_model_call(self):
        class Counting:
            calls = 0
            def choose(self, text):
                self.calls += 1
                return "inspect-release"
        planner = Counting(); planned = []
        intake = Inbox(self.root / "intake", registry(self.config, self.snapshot, intake=True))
        intake.accept(self.value)
        def lost(value):
            planned.append(value)
            raise TimeoutError("synthetic ingress ack loss")
        with self.assertRaises(TimeoutError):
            intake.plan(planner, lost)
        intake.plan(planner, planned.append)
        self.assertEqual(planner.calls, 1)
        self.assertEqual(planned[0], planned[1])
        self.assertEqual(planned[0]["actor"], self.value["actor"])

    def test_model_cannot_propose_shell_or_replace_sender(self):
        class Forged:
            def choose(self, text):
                return "shell"
        intake = Inbox(self.root / "intake", registry(self.config, self.snapshot, intake=True))
        intake.accept(self.value)
        with self.assertRaises(Rejected):
            intake.plan(Forged(), self.fail)

    def test_changed_snapshot_is_rejected_before_local_execution(self):
        worker, client, executor = self.service()
        client.call("/v1/twin/ingress", self.value); worker.step()
        claim = worker.claim("host")
        (self.project / "releases.json").write_text("[]")
        with self.assertRaisesRegex(Rejected, "snapshot_changed"):
            host(self.root / "host", self.config, self.snapshot, self.project).execute(claim)
        with self.assertRaisesRegex(Rejected, "unknown"):
            host(self.root / "host", self.config, self.snapshot, self.project).execute(claim)

    def test_offline_ledgers_cannot_be_silently_switched_to_real_receipts(self):
        Worker(self.root / "old-worker")
        with self.assertRaisesRegex(Rejected, "migration_required"):
            Worker(self.root / "old-worker", receipt_kind="feishu")
        Worker(self.root / "new-worker", receipt_kind="feishu")
        with self.assertRaisesRegex(Rejected, "profile_changed"):
            Worker(self.root / "new-worker")

    def test_config_rejects_inline_secrets_extra_powers_and_networked_offline_planner(self):
        for key in ["app_secret", "shell", "project_path"]:
            bad = copy.deepcopy(self.config); bad[key] = "synthetic"
            with self.subTest(key=key), self.assertRaises(Rejected):
                validate(bad)
        bad = copy.deepcopy(self.config); bad["planner"] = {"provider": "deepseek", "model": "deepseek-flash", "api_key_env": "IM_AGENT_MODEL_KEY"}
        with self.assertRaises(Rejected):
            validate(bad)
        with patch("urllib.request.build_opener", side_effect=AssertionError("offline must not connect")):
            self.assertEqual(Planner(self.config).choose("检查发布"), "inspect-release")
