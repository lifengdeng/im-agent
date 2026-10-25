import copy
from pathlib import Path
import tempfile
import unittest

from im_agent.adapters import Registry
from im_agent.contracts import Rejected, fixture
from im_agent.ledger import Host, Worker


def descriptor(snapshot="release-a"):
    return {"project": "local-project", "capability": "describe", "adapter": "local-readonly-v1", "snapshot": snapshot,
            "actors": [fixture()["actor"]],
            "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            "result_schema": {"type": "object", "properties": {"verified": {"type": "boolean", "enum": [True]},
                "version": {"type": "string", "maxLength": 80}}, "required": ["verified", "version"], "additionalProperties": False},
            "expected_result": {"verified": True, "version": snapshot}}


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.registry = Registry(); self.root = Path(self.tmp.name)
        self.value = dict(fixture(), project="local-project", capability="describe", input={})

    def test_other_project_runs_registered_handler_through_pinned_contract(self):
        d = descriptor(); calls = []
        self.registry.add(d, lambda value: calls.append(value) or d["expected_result"])
        worker = Worker(self.root / "worker.sqlite3", self.registry)
        worker.ingress(self.value)
        claim = worker.claim("host")
        result = Host(self.root / "host.sqlite3", self.registry).execute(claim)
        worker.finish({**{k: claim[k] for k in ["execution", "executor", "lease", "binding"]}, "result": result})
        self.assertEqual(calls, [{}])
        self.assertEqual(worker.delivery_intents()[0]["result"]["version"], "release-a")

    def test_snapshot_update_pins_already_queued_and_running_jobs(self):
        a = descriptor(); self.registry.add(a, lambda value: a["expected_result"])
        worker = Worker(self.root / "worker.sqlite3", self.registry)
        worker.ingress(self.value)
        b = descriptor("release-b"); self.registry.add(b, lambda value: b["expected_result"])
        worker.ingress(dict(self.value, id="second"))
        self.assertTrue(worker.ingress(self.value)["duplicate"])
        host = Host(self.root / "host.sqlite3", self.registry)
        for version in ["release-a", "release-b"]:
            claim = worker.claim("host")
            self.assertEqual(claim["binding"]["snapshot"], version)
            result = host.execute(claim)
            self.assertEqual(result["version"], version)
            worker.finish({**{k: claim[k] for k in ["execution", "executor", "lease", "binding"]}, "result": result})

    def test_registration_does_not_allow_actor_or_input_expansion(self):
        self.registry.add(descriptor())
        worker = Worker(self.root / "worker.sqlite3", self.registry)
        for value in [dict(self.value, actor=dict(self.value["actor"], user="other")),
                      dict(self.value, input={"command": "not-registered"}), dict(self.value, capability="shell")]:
            with self.assertRaises(Rejected): worker.ingress(value)

    def test_wrong_snapshot_and_unverified_data_are_rejected(self):
        d = descriptor(); self.registry.add(d, lambda value: dict(d["expected_result"], version="invented"))
        worker = Worker(self.root / "worker.sqlite3", self.registry); worker.ingress(self.value)
        claim = worker.claim("host"); host = Host(self.root / "host.sqlite3", self.registry)
        altered = copy.deepcopy(claim); altered["binding"]["snapshot"] = "release-b"
        with self.assertRaises(Rejected): host.execute(altered)
        with self.assertRaises(Rejected): host.execute(claim)
        with self.assertRaisesRegex(Rejected, "unknown"):
            host.execute(claim)

    def test_unknown_schema_features_fail_closed(self):
        d = descriptor(); d["input_schema"]["oneOf"] = []
        with self.assertRaisesRegex(Rejected, "unsupported_schema"):
            self.registry.add(d)

    def test_current_actor_revocation_also_blocks_pinned_job(self):
        self.registry.add(descriptor())
        worker = Worker(self.root / "worker.sqlite3", self.registry); worker.ingress(self.value)
        claim = worker.claim("host")
        updated = descriptor("release-b"); updated["actors"] = [dict(self.value["actor"], user="other")]
        self.registry.add(updated)
        with self.assertRaises(Rejected): Host(self.root / "host.sqlite3", self.registry).execute(claim)

    def test_group_scope_keeps_sender_identity_and_all_other_boundaries(self):
        d=descriptor();d['actors']=[]
        d['group_scopes']=[{k:v for k,v in self.value['actor'].items() if k!='user'}]
        calls=[]
        self.registry.add(d,lambda event:calls.append(event) or d['expected_result'],contextual=True)
        value=copy.deepcopy(self.value);value['actor']['user']='another-verified-member'
        binding=self.registry.binding(value)
        self.assertEqual(self.registry.execute(value,binding),d['expected_result'])
        self.assertEqual(calls,[value])
        for field in ['tenant','chat','instance','agent','kind']:
            bad=copy.deepcopy(value);bad['actor'][field]='private' if field=='kind' else 'different'
            with self.assertRaises(Rejected):self.registry.binding(bad)
        changed=copy.deepcopy(d);changed['group_scopes'][0]['chat']='another-room'
        self.assertNotEqual(self.registry.policy(d),self.registry.policy(changed))

    def test_group_scope_cannot_be_a_private_or_partial_wildcard(self):
        for scope in [{'kind':'group'},dict(self.value['actor']),{k:('private' if k=='kind' else v) for k,v in self.value['actor'].items() if k!='user'}]:
            d=descriptor();d['group_scopes']=[scope]
            with self.assertRaises(Rejected):self.registry.add(d)
