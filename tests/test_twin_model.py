"""Synthetic provider responses verify routing, audit durability and no paid retries."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from im_agent.bundles import inventory
from im_agent.contracts import Rejected, fingerprint
from im_agent.feishu import normalize
from im_agent.intake import Inbox
from im_agent.twin import TwinWorker
from im_agent.twin_config import load, registry
from im_agent.twin_planner import Planner

ROOT = Path(__file__).resolve().parents[1]
KEY = "synthetic-provider-key-for-offline-tests"


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = load(ROOT / "examples/digital-twin/instance.example.json")
        self.config["profile"] = "feishu"
        self.config["feishu"]["accept_after_ms"] = 1
        self.config["planner"] = {"provider": "deepseek", "model": "deepseek-flash", "api_key_env": "IM_AGENT_MODEL_TEST_KEY"}
        raw = json.loads((ROOT / "examples/digital-twin/message.example.json").read_text(encoding="utf-8"))
        raw["event"]["message"]["create_time"] = "2"
        self.value = normalize(raw, self.config)
        self.snapshot = fingerprint(inventory(ROOT / "examples/digital-twin/project"))
        self.reg = registry(self.config, self.snapshot, intake=True)
        self.environment = patch.dict("os.environ", {"IM_AGENT_MODEL_TEST_KEY": KEY})
        self.environment.start(); self.addCleanup(self.environment.stop)

    def completion(self, capability="inspect-release"):
        return {"id": "synthetic-completion", "model": "deepseek-flash", "choices": [{"finish_reason": "stop",
            "message": {"role": "assistant", "content": json.dumps({"capability": capability})}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 8, "total_tokens": 108}}

    def http(self, body):
        opener = Mock()
        opener.open.return_value = io.BytesIO(json.dumps(body).encode())
        return opener

    def test_verified_routing_keeps_original_authority_and_audit_after_restart(self):
        worker = TwinWorker(self.root / "worker", self.config, self.snapshot)
        worker.intake.accept(self.value); http = self.http(self.completion())
        with patch("urllib.request.build_opener", return_value=http):
            worker.step()
        request = http.open.call_args.args[0]; body = json.loads(request.data)
        self.assertEqual(request.full_url, "https://api.deepseek.com/chat/completions")
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["max_tokens"], 128)
        self.assertEqual(body["messages"][-1], {"role": "user", "content": self.value["input"]["text"]})
        self.assertNotIn(self.value["actor"]["user"], request.data.decode())
        restarted = TwinWorker(self.root / "worker", self.config, self.snapshot)
        restarted.intake.accept(self.value)
        with patch("urllib.request.build_opener", side_effect=AssertionError("must not call again")):
            restarted.step()
        self.assertEqual(restarted.claim("synthetic-host")["event"], dict(self.value, capability="inspect-release"))
        self.assertEqual(restarted.status()["model_calls"], 1)
        self.assertEqual(restarted.status()["model_attempts"], {"verified": 1})
        with restarted.intake.connect() as db:
            receipt = json.loads(db.execute("SELECT receipt FROM model_attempts").fetchone()[0])
        self.assertEqual(receipt["usage"]["total_tokens"], 108)
        self.assertEqual(receipt["response_id_fingerprint"], fingerprint("synthetic-completion"))
        self.assertNotIn(KEY, json.dumps(receipt))
        self.assertNotIn("messages", receipt)

    def test_rejected_provider_output_never_queues_work_or_retries(self):
        bodies = []
        for plan in [{"capability": "shell"}, {"capability": "inspect-release", "actor": "forged"},
                     {"capability": "inspect-release", "path": "/unreviewed"}]:
            body = self.completion(); body["choices"][0]["message"]["content"] = json.dumps(plan); bodies.append(body)
        truncated = self.completion(); truncated["choices"][0]["finish_reason"] = "length"; bodies.append(truncated)
        usage = self.completion(); usage["usage"]["completion_tokens"] = True; bodies.append(usage)
        sensitive = self.completion(); sensitive["model"] = KEY; bodies.append(sensitive)
        for index, body in enumerate(bodies):
            with self.subTest(index=index):
                root = self.root / str(index); worker = TwinWorker(root, self.config, self.snapshot)
                worker.intake.accept(self.value)
                with patch("urllib.request.build_opener", return_value=self.http(body)):
                    with self.assertRaises(Rejected): worker.step()
                restarted = TwinWorker(root, self.config, self.snapshot)
                with patch("urllib.request.build_opener", side_effect=AssertionError("must not retry")):
                    restarted.intake.accept(self.value); restarted.step()
                self.assertIsNone(restarted.claim("synthetic-host"))
                self.assertEqual(restarted.status()["model_attempts"], {"unknown": 1})
                with restarted.intake.connect() as db:
                    self.assertIsNone(db.execute("SELECT receipt FROM model_attempts").fetchone()[0])

    def test_accepted_plan_retries_ingress_without_another_provider_call(self):
        intake = Inbox(self.root / "intake", self.reg, recover_planning=True); intake.accept(self.value)
        planner = Planner(self.config); http = self.http(self.completion("help"))
        with patch("urllib.request.build_opener", return_value=http):
            with self.assertRaises(TimeoutError): intake.plan(planner, Mock(side_effect=TimeoutError()))
        restarted = Inbox(intake.path, self.reg, recover_planning=True); submitted = []
        with patch("urllib.request.build_opener", side_effect=AssertionError("must not call again")):
            restarted.plan(Planner(self.config), submitted.append)
        self.assertEqual(http.open.call_count, 1)
        self.assertEqual(submitted, [dict(self.value, capability="help")])
        self.assertEqual(restarted.model_status(), {"verified": 1})

    def test_interrupted_attempt_remains_unknown_after_worker_reconstruction(self):
        intake = Inbox(self.root / "intake", self.reg, recover_planning=True); intake.accept(self.value)
        http = Mock(); http.open.side_effect = KeyboardInterrupt()
        with patch("urllib.request.build_opener", return_value=http):
            with self.assertRaises(KeyboardInterrupt): intake.plan(Planner(self.config), self.fail)
        restarted = Inbox(intake.path, self.reg, recover_planning=True)
        with patch("urllib.request.build_opener", side_effect=AssertionError("must not retry")):
            restarted.accept(self.value); restarted.plan(Planner(self.config), self.fail)
        self.assertEqual(restarted.status(), {"unknown": 1})
        self.assertEqual(restarted.model_status(), {"unknown": 1})

    def test_failure_keeps_provider_error_and_credentials_out_of_receipt(self):
        planner = Planner(self.config); http = Mock(); http.open.side_effect = OSError(KEY)
        with patch("urllib.request.build_opener", return_value=http):
            with self.assertRaises(Rejected) as caught: planner.choose("检查示例项目最近一次发布")
        self.assertNotIn(KEY, str(caught.exception)); self.assertIsNone(planner.last_receipt)
        self.assertEqual(http.open.call_count, 1)
