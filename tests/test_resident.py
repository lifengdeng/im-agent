import copy
import tempfile
from pathlib import Path
import unittest
import threading

from im_agent.adapters import Registry, echo_descriptor
from im_agent.contracts import Rejected, fixture
from im_agent.ledger import Host, Worker
from im_agent.resident import ProcessLock, VersionFollower


class VersionTests(unittest.TestCase):
    def test_publication_pins_old_jobs_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); d = echo_descriptor(); d["adapter"] = "versioned"
            worker = Worker(root / "worker", Registry({"schema": 1, "entries": [d]}))
            old = fixture("old"); worker.ingress(old)
            newer = copy.deepcopy(d); newer["snapshot"] = "new"
            worker.publish({"generation": 1, "declarations": {"schema": 1, "entries": [newer]}})
            worker.ingress(fixture("new"))
            restored = Worker(root / "worker", Registry({"schema": 1, "entries": [d]}))
            self.assertEqual(restored.claim("host")["binding"]["snapshot"], "builtin-v1")
            self.assertEqual(restored.claim("host")["binding"]["snapshot"], "new")
            with self.assertRaises(Rejected): restored.publish({"generation": 0, "declarations": {"schema": 1, "entries": [d]}})
            newer["actors"].append(dict(fixture()["actor"], user="another"))
            with self.assertRaises(Rejected): restored.publish({"generation": 2, "declarations": {"schema": 1, "entries": [newer]}})

    def test_internal_results_never_become_channel_deliveries(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = Worker(Path(tmp) / "w"); h = Host(Path(tmp) / "h")
            e = fixture(); w.submit_internal(e, "conversation")
            with self.assertRaises(Rejected): w.ingress(e)
            claim = w.claim("host"); result = h.execute(claim)
            binding = {k: claim[k] for k in ("execution", "executor", "lease", "binding")}
            w.finish(dict(binding, result=result))
            self.assertEqual(w.internal(e["id"], "conversation")["result"], result)
            self.assertEqual(w.delivery_intents(), [])
            with self.assertRaises(Rejected): w.internal(e["id"], "other")

    def test_failed_watch_does_not_block_old_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); host = Host(root / "h")
            def failed(): raise Rejected("candidate_failed")
            follower = VersionFollower(lambda: "update", failed, host, None, root / "watch.json")
            self.assertFalse(follower.step())
            w = Worker(root / "w"); w.ingress(fixture())
            self.assertTrue(host.execute(w.claim("host"))["verified"])

    def test_os_lock_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lock"
            with ProcessLock(path):
                with self.assertRaises(Rejected):
                    with ProcessLock(path): pass
            with ProcessLock(path): pass

    def test_slow_candidate_and_unknown_publication_keep_old_intake(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); worker = Worker(root / "w"); host = Host(root / "h")
            entered, proceed = threading.Event(), threading.Event()
            d = echo_descriptor(); d["snapshot"] = "next"
            incoming = Registry(); incoming.add(d, lambda args: {"verified": True, "text": args["text"], "artifacts": []})
            def prepare():
                entered.set(); proceed.wait(3)
                return {"host_registry": incoming, "declarations": incoming.declarations(), "details": {}}
            class Client:
                lost = True
                def call(self, path, value):
                    if path == "/v1/registry": return worker.registration()
                    result = worker.publish(value)
                    if self.lost:
                        self.lost = False; raise OSError("response lost after durable commit")
                    return result
            follower = VersionFollower(lambda: "candidate", prepare, host, Client(), root / "watch")
            thread = threading.Thread(target=follower.step); thread.start(); self.assertTrue(entered.wait(2))
            worker.ingress(fixture("during-copy")); old = worker.claim("host")
            self.assertTrue(host.execute(old)["verified"])
            proceed.set(); thread.join(3)
            self.assertIsNotNone(follower.pending)
            self.assertTrue(follower.step()); self.assertTrue(follower.ready.is_set())
            self.assertEqual(worker.registration()["generation"], 1)


if __name__ == "__main__": unittest.main()
