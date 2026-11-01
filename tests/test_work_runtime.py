import copy
from pathlib import Path
import tempfile
import unittest

from im_agent.adapters import Registry, echo_descriptor
from im_agent.contracts import Rejected, fixture
from im_agent.ledger import Database, Worker
from im_agent.work_runtime import Proactive, Resources, Watchdog, install


def policy(mode='enabled'):
    return {'id':'readonly-rule','revision':'v1','source':'health','kinds':['failure'],
            'capability':'diagnose','mode':mode,'hourly_budget':2}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.db=Database(self.root/'work.sqlite3','synthetic-work')
        with self.db.connect() as db:install(db)

    def test_resource_conflicts_same_item_and_bounded_independent_runs(self):
        with self.db.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.assertTrue(Resources.acquire(db,'a','one',['shared']))
            self.assertFalse(Resources.acquire(db,'a','two',['other']))
            self.assertFalse(Resources.acquire(db,'b','three',['shared']))
            self.assertTrue(Resources.acquire(db,'b','four',['other']))
            self.assertFalse(Resources.acquire(db,'c','five',[]))
            self.assertTrue(Resources.acquire(db,'a','one',['shared']))
            with self.assertRaises(Rejected):Resources.acquire(db,'b','one',['shared'])

    def test_unknown_reserves_resources_until_explicit_proof_and_never_replays(self):
        with self.db.connect() as db:
            Resources.acquire(db,'a','one',['shared'],limit=1);Resources.finish(db,'one','unknown')
            self.assertFalse(Resources.acquire(db,'b','two',['other'],limit=1))
            with self.assertRaises(Rejected):Resources.finish(db,'one','closed')
            Resources.finish(db,'one','closed',proof='a'*64)
            with self.assertRaises(Rejected):Resources.finish(db,'one','closed',proof='b'*64)
            with self.assertRaises(Rejected):Resources.finish(db,'one','unknown')
            self.assertFalse(Resources.acquire(db,'a','one',['shared']))
            self.assertTrue(Resources.acquire(db,'b','two',['shared'],limit=1))

    def source(self, key='one'):
        return {'id':key,'actor':fixture()['actor'],'source':'health','kind':'failure'}

    def test_shadow_off_and_budget_do_not_start_unbounded_work(self):
        with self.db.connect() as db:
            off=Proactive([policy('off')]);self.assertIsNone(off.propose(db,'readonly-rule',self.source(),'case',now=100))
            shadow=Proactive([policy('shadow')]);r=shadow.propose(db,'readonly-rule',self.source(),'case',now=100)
            self.assertEqual(r['state'],'shadow');self.assertIsNone(shadow.claim(db,r['id']))
            rules=Proactive([policy()])
            for key,state in [('a','queued'),('b','queued'),('c','budget_exhausted')]:
                row=rules.propose(db,'readonly-rule',self.source(key),'case',now=100)
                self.assertEqual(row['state'],state)
                self.assertEqual(rules.propose(db,'readonly-rule',self.source(key),'case',now=100),row)

    def test_restart_unknown_and_policy_change_cannot_replay_action(self):
        with self.db.connect() as db:
            rules=Proactive([policy()]);r=rules.propose(db,'readonly-rule',self.source(),'case')
            self.assertIsNotNone(rules.claim(db,r['id']));self.assertIsNone(rules.claim(db,r['id']))
            rules.recover(db);self.assertIsNone(rules.claim(db,r['id']))
            with self.assertRaises(Rejected):rules.finish(db,r['id'],{'verified':True})
            r=rules.propose(db,'readonly-rule',self.source('new'),'case')
            self.assertIsNone(Proactive([policy('shadow')]).claim(db,r['id']))

    def test_explicit_case_budgets_allow_independent_work_without_replenishing_unknown(self):
        rules=Proactive([dict(policy(),budget_scope='case')])
        with self.db.connect() as db:
            for key in ['first','second']:
                row=rules.propose(db,'readonly-rule',self.source(key),'case-a',now=100)
                self.assertEqual(row['state'],'queued')
            with self.assertRaises(Rejected):rules.propose(db,'readonly-rule',self.source('second'),'case-b',now=100)
            rules.claim(db,row['id']);rules.recover(db)
            self.assertEqual(rules.propose(db,'readonly-rule',self.source('third'),'case-a',now=101)['state'],'budget_exhausted')
            self.assertEqual(rules.propose(db,'readonly-rule',self.source('other'),'case-b',now=101)['state'],'queued')
            self.assertEqual(rules.propose(db,'readonly-rule',self.source('later'),'case-a',now=3701)['state'],'queued')

    def test_policy_budget_remains_global_and_unknown_budget_scope_is_rejected(self):
        with self.assertRaises(Rejected):Proactive([dict(policy(),budget_scope='actor')])
        rules=Proactive([policy()])
        with self.db.connect() as db:
            for i in range(2):rules.propose(db,'readonly-rule',self.source(str(i)),'case-a',now=100)
            self.assertEqual(rules.propose(db,'readonly-rule',self.source('third'),'case-b',now=101)['state'],'budget_exhausted')

    def test_verified_result_and_current_policy_binding(self):
        with self.db.connect() as db:
            rules=Proactive([policy()]);r=rules.propose(db,'readonly-rule',self.source(),'case')
            claimed=rules.claim(db,r['id']);self.assertEqual(claimed['policy'],policy())
            rules.finish(db,r['id'],{'verified':True,'repair_authorized':False})
            self.assertEqual(db.execute('SELECT state FROM proactive_candidates').fetchone()[0],'ready')

    def test_watchdog_uses_real_progress_and_reports_each_transition_once(self):
        with self.db.connect() as db:
            self.assertIsNone(Watchdog.observe(db,'item','running',[],now=100,stall_after=60))
            self.assertIsNone(Watchdog.observe(db,'item','running',[],now=159,stall_after=60))
            alert=Watchdog.observe(db,'item','running',[],now=160,stall_after=60)
            self.assertEqual(alert['kind'],'no_progress');self.assertFalse(alert['repair_authorized'])
            self.assertIsNone(Watchdog.observe(db,'item','running',[],now=161,stall_after=60))
            self.assertEqual(Watchdog.observe(db,'item','running',['checkpoint'],now=162)['kind'],'recovered')
            self.assertEqual(Watchdog.observe(db,'item','unknown',[],now=163)['kind'],'reconciliation_required')
            self.assertIsNone(Watchdog.observe(db,'item','unknown',[],now=9000))
            self.assertEqual(Watchdog.observe(db,'item','waiting_auth',[],now=9001)['kind'],'authentication_required')

    def test_explicit_capability_addition_preserves_pinned_jobs_and_scope(self):
        path=self.root/'worker.sqlite3';old=echo_descriptor();worker=Worker(path,Registry({'schema':1,'entries':[old]}))
        worker.ingress(fixture('old'))
        new=copy.deepcopy(old);new.update(capability='echo-isolated',adapter='isolated-v1')
        registry=Registry({'schema':1,'entries':[old,new]});mapping={('example','echo-isolated'):('example','echo')}
        with self.assertRaises(Rejected):Worker(path,registry)
        widened=copy.deepcopy(new);widened['actors'].append(dict(fixture()['actor'],user='other'))
        with self.assertRaises(Rejected):Worker(path,Registry({'schema':1,'entries':[old,widened]}),capability_additions=mapping)
        migrated=Worker(path,registry,capability_additions=mapping)
        self.assertEqual(migrated.registration()['generation'],1)
        self.assertEqual(migrated.claim('executor')['binding']['snapshot'],'builtin-v1')
        self.assertEqual(Worker(path,registry).registration()['generation'],1)
        with self.assertRaises(Rejected):Worker(path,Registry({'schema':1,'entries':[old]}))
