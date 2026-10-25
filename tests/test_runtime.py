import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error

from im_agent.contracts import Rejected, fixture
from im_agent.ledger import Host, Transport, Worker
from im_agent.memory import Memory
from im_agent.service import Client, execute_once, run_transport, server


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.worker = Worker(self.root / "worker.sqlite3")

    def claimed(self):
        self.worker.ingress(fixture())
        claim = self.worker.claim("executor")
        return claim, {k: claim[k] for k in ("execution", "executor", "lease", "binding")}

    def test_ingress_collision_and_model_cannot_select_shell(self):
        self.worker.ingress(fixture())
        self.assertTrue(self.worker.ingress(fixture())["duplicate"])
        forged = fixture(); forged["input"]["text"] = "different"
        with self.assertRaises(Rejected): self.worker.ingress(forged)
        forged = fixture(); forged["capability"] = "shell"
        with self.assertRaises(Rejected): self.worker.ingress(forged)

    def test_atomic_claim_only_one_executor(self):
        self.worker.ingress(fixture())
        barrier = threading.Barrier(3); claims = []
        def take(name):
            barrier.wait(); claims.append(self.worker.claim(name))
        threads = [threading.Thread(target=take, args=(name,)) for name in ("one", "two")]
        for thread in threads: thread.start()
        barrier.wait()
        for thread in threads: thread.join()
        self.assertEqual(sum(c is not None for c in claims), 1)

    def test_expiry_becomes_unknown_and_not_replayed_after_restart(self):
        self.worker.ingress(fixture())
        self.worker.claim("executor", now=1)
        self.assertIsNone(self.worker.claim("executor", now=100))
        restarted = Worker(self.worker.path)
        self.assertEqual(restarted.status()["jobs"], {"unknown": 1})
        self.assertIsNone(restarted.claim("executor"))

    def test_progress_order_duplicate_and_binding(self):
        claim, binding = self.claimed()
        progress = dict(binding, seq=1, phase="started")
        self.assertFalse(self.worker.progress(progress)["duplicate"])
        self.assertTrue(self.worker.progress(progress)["duplicate"])
        for bad in [dict(progress, phase="verified"), dict(progress, seq=3), dict(progress, executor="other")]:
            with self.assertRaises(Rejected): self.worker.progress(bad)

    def test_heartbeat_renews_only_the_bound_live_execution(self):
        _, binding = self.claimed()
        with self.worker.connect() as db:
            db.execute("UPDATE jobs SET expires=expires-20")
            before = db.execute("SELECT expires FROM jobs").fetchone()[0]
        self.assertTrue(self.worker.heartbeat(binding)["accepted"])
        with self.worker.connect() as db:
            self.assertGreater(db.execute("SELECT expires FROM jobs").fetchone()[0], before)
        with self.assertRaisesRegex(Rejected, "binding_rejected"):
            self.worker.heartbeat(dict(binding, lease="other-lease"))

    def test_heartbeat_cannot_revive_an_expired_execution(self):
        _, binding = self.claimed()
        with self.worker.connect() as db: db.execute("UPDATE jobs SET expires=1")
        with self.assertRaisesRegex(Rejected, "expired_unknown"): self.worker.heartbeat(binding)
        self.assertIsNone(self.worker.claim("other-executor"))
        self.assertEqual(self.worker.status()["jobs"], {"unknown": 1})

    def test_result_verified_and_receipt_destination_bound(self):
        claim, binding = self.claimed()
        with self.assertRaises(Rejected): self.worker.finish(dict(binding, result={"verified": True, "text": "made up", "artifacts": []}))
        result = {"verified": True, "text": claim["event"]["input"]["text"], "artifacts": []}
        self.worker.finish(dict(binding, result=result))
        self.assertTrue(self.worker.finish(dict(binding, result=result))["duplicate"])
        transport = Transport(self.root / "transport.sqlite3")
        intent = self.worker.delivery_intents()[0]
        calls = []
        def sink(item): calls.append(item); return "fake-" + item["id"]
        receipt = transport.deliver(intent, sink)
        self.assertEqual(transport.deliver(intent, sink), receipt)
        self.assertEqual(len(calls), 1)
        self.worker.receipt(receipt)
        self.assertEqual(self.worker.delivery_intents(), [])
        with self.assertRaises(Rejected): self.worker.receipt(dict(receipt, digest="wrong"))

    def test_unknown_delivery_does_not_send_twice(self):
        transport = Transport(self.root / "transport.sqlite3")
        def timeout(item): raise TimeoutError("fake accepted but receipt lost")
        with self.assertRaises(TimeoutError): transport.deliver({"id": "event"}, timeout)
        restarted = Transport(transport.path)
        with self.assertRaisesRegex(Rejected, "unknown_delivery"):
            restarted.deliver({"id": "event"}, lambda item: self.fail("must not resend"))

    def test_host_result_survives_retry_without_running_adapter(self):
        claim, _ = self.claimed(); host = Host(self.root / "host.sqlite3")
        result = host.execute(claim, lambda args: {"verified": True, "text": args["text"], "artifacts": []})
        restarted = Host(host.path)
        self.assertEqual(restarted.execute(claim, lambda args: self.fail("replayed")), result)
        claim["binding"]["snapshot"] = "different"
        with self.assertRaises(Rejected): restarted.execute(claim, lambda args: {})

    def test_database_ownership_cannot_be_shared(self):
        with self.assertRaises(Rejected): Transport(self.worker.path)

    def test_chinese_input_survives_worker_host_and_delivery_ledgers(self):
        value = fixture(); value["input"]["text"] = "中文工程名、花名与核验结果"
        self.worker.ingress(value)
        claim = self.worker.claim("executor")
        result = Host(self.root / "host.sqlite3").execute(claim, lambda args: {"verified": True, "text": args["text"], "artifacts": []})
        binding = {k: claim[k] for k in ("execution", "executor", "lease", "binding")}
        self.worker.finish(dict(binding, result=result))
        self.assertEqual(self.worker.delivery_intents()[0]["result"]["text"], value["input"]["text"])

    def test_loopback_api_roles_and_transport_host_roundtrip(self):
        tokens = {"transport": "t" * 40, "executor": "e" * 40}
        http = server(("127.0.0.1", 0), self.worker, tokens)
        thread = threading.Thread(target=http.serve_forever, daemon=True); thread.start()
        self.addCleanup(http.server_close); self.addCleanup(http.shutdown)
        url = "http://127.0.0.1:" + str(http.server_port)
        client = Client(url, tokens["transport"])
        with self.assertRaises(urllib.error.HTTPError): client.call("/v1/claim", {"executor": "stolen"})
        errors = []
        def transport():
            try: run_transport(client, self.root / "transport")
            except Exception as exc: errors.append(exc)
        (self.root / "transport").mkdir()
        transport_thread = threading.Thread(target=transport); transport_thread.start()
        executor = Client(url, tokens["executor"])
        import time
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not execute_once(executor, Host(self.root / "host.sqlite3")):
            time.sleep(0.05)
        transport_thread.join(timeout=10)
        self.assertFalse(transport_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.worker.status()["delivered"], 1)

    def test_authenticated_artifact_payload_is_bounded_before_dispatch(self):
        from im_agent.service import MAX_REQUEST_BYTES
        tokens={"transport":"t"*40,"executor":"e"*40};seen=[]
        http=server(("127.0.0.1",0),self.worker,tokens,{"/v1/chunk":("executor",lambda v:seen.append(len(v['data'])) or {"accepted":True})})
        threading.Thread(target=http.serve_forever,daemon=True).start()
        self.addCleanup(http.server_close);self.addCleanup(http.shutdown)
        client=Client("http://127.0.0.1:"+str(http.server_port),tokens['executor'])
        self.assertTrue(client.call('/v1/chunk',{'data':'a'*65536})['accepted'])
        with self.assertRaises(urllib.error.HTTPError):client.call('/v1/chunk',{'data':'a'*MAX_REQUEST_BYTES})
        self.assertEqual(seen,[65536])

    def test_offline_client_refuses_external_address(self):
        for url in ["https://example.com", "http://example.com", "http://localhost@evil.test", "http://127.0.0.1/?token=bad"]:
            with self.assertRaises(Rejected): Client(url, "t" * 40)


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.memory = Memory(Path(self.tmp.name) / "memory.sqlite3")
        self.who = fixture()["actor"]

    def test_scopes_filter_before_recall(self):
        self.memory.change(self.who, "voice", "brief", "one", explicit=True)
        self.assertEqual(self.memory.recall(self.who)[0]["value"], "brief")
        for field in ["instance", "agent", "tenant", "user", "chat", "kind"]:
            changed = dict(self.who, **{field: "private" if field == "kind" else "other"})
            self.assertEqual(self.memory.recall(changed), [])
        self.assertEqual(self.memory.recall(self.who, shared=True), [])

    def test_explicit_correction_forget_and_replay(self):
        with self.assertRaises(Rejected): self.memory.change(self.who, "voice", "brief", "one")
        self.memory.change(self.who, "voice", "brief", "one", explicit=True)
        receipt = self.memory.change(self.who, "voice", "detailed", "two", explicit=True)
        self.assertEqual(receipt["revision"], 2)
        self.memory.change(self.who, "voice", "", "three", explicit=True, forget=True)
        self.memory.change(self.who, "voice", "brief", "one", explicit=True)
        self.assertEqual(self.memory.recall(self.who), [])
        with self.memory.connect() as db:
            self.assertIsNone(db.execute("SELECT value FROM memories").fetchone()[0])

    def test_shared_memory_cannot_be_changed_by_other_member(self):
        self.memory.change(self.who, "label", "demo", "one", explicit=True, shared=True)
        other = dict(self.who, user="bob")
        self.assertEqual(self.memory.recall(other, shared=True)[0]["value"], "demo")
        with self.assertRaises(Rejected): self.memory.change(other, "label", "", "two", shared=True, explicit=True, forget=True)

    def test_restore_applies_delete_tombstone(self):
        self.memory.change(self.who, "voice", "brief", "one", explicit=True)
        backup = Path(self.tmp.name) / "backup.sqlite3"
        with self.memory.connect() as source:
            import sqlite3
            target = sqlite3.connect(backup); source.backup(target); target.close()
        self.memory.change(self.who, "voice", "", "two", explicit=True, forget=True)
        restored = Memory(backup); restored.apply_tombstones(self.memory.tombstones())
        self.assertEqual(restored.recall(self.who), [])
