import copy
from pathlib import Path
import tempfile
import threading
import time
import unittest

from im_agent.adapters import Registry,echo_descriptor
from im_agent.contracts import Rejected,fixture
from im_agent.execution import ExecutionPool
from im_agent.ledger import Host,Worker
from im_agent.service import Client,server


class LocalClient:
    def __init__(self,worker):self.worker=worker;self.calls=[];self.lock=threading.Lock()
    def call(self,path,value):
        with self.lock:self.calls.append((path,copy.deepcopy(value)))
        if path=='/v1/claim':return self.worker.claim(value['executor'])
        return {'/v1/progress':self.worker.progress,'/v1/heartbeat':self.worker.heartbeat,'/v1/result':self.worker.finish}[path](value)


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.gates={};self.entered={};self.executed=[]
        self.descriptor=echo_descriptor();self.descriptor['adapter']='parallel-test'
        self.registry=Registry({'schema':1,'entries':[self.descriptor]})
        self.registry.add(self.descriptor,self.handle,contextual=True)
        self.worker=Worker(self.root/'worker',self.registry);self.host=Host(self.root/'host',self.registry)
        self.client=LocalClient(self.worker)
        self.pool=ExecutionPool(self.client,self.host,heartbeat_interval=.02)
        self.addCleanup(self.finish)

    def handle(self,event):
        key=event['id'];self.executed.append(key);self.entered[key].set()
        if not self.gates[key].wait(5):raise AssertionError('synthetic handler was not released')
        return {'verified':True,'text':event['input']['text'],'artifacts':[]}

    def add(self,key):
        self.gates[key]=threading.Event();self.entered[key]=threading.Event();self.worker.ingress(fixture(key))

    def finish(self):
        for gate in self.gates.values():gate.set()
        self.pool.close()

    def wait(self,predicate):
        end=time.monotonic()+3
        while time.monotonic()<end:
            if predicate():return
            time.sleep(.005)
        self.assertTrue(predicate(),'synthetic execution did not settle')

    def test_two_execute_concurrently_third_waits_without_blocking_dispatcher(self):
        for key in ['a','b','c']:self.add(key)
        self.assertEqual(self.pool.step(),2)
        self.assertTrue(self.entered['a'].wait(2));self.assertTrue(self.entered['b'].wait(2))
        self.assertFalse(self.entered['c'].is_set());self.assertEqual(self.pool.step(),0)
        self.gates['a'].set();self.wait(lambda:self.pool.status()['completed']==1)
        self.assertEqual(self.pool.step(),1);self.assertTrue(self.entered['c'].wait(2))
        self.assertFalse(self.gates['b'].is_set());self.assertEqual(self.pool.status()['active'],2)
        self.wait(lambda:len([c for c in self.client.calls if c[0]=='/v1/heartbeat'])>=2)
        for path,value in self.client.calls:
            if path=='/v1/heartbeat':self.assertEqual(value['executor'],'pooled-host')

    def test_http_client_and_host_ledgers_support_parallel_leases(self):
        for key in ['a','b']:self.add(key)
        tokens={'executor':'e'*40,'transport':'t'*40}
        http=server(('127.0.0.1',0),self.worker,tokens)
        serving=threading.Thread(target=http.serve_forever,daemon=True);serving.start()
        self.addCleanup(http.server_close);self.addCleanup(http.shutdown)
        self.pool.client=Client('http://127.0.0.1:'+str(http.server_port),tokens['executor'])
        self.assertEqual(self.pool.step(),2)
        for key in ['a','b']:self.assertTrue(self.entered[key].wait(2))
        for gate in self.gates.values():gate.set()
        self.wait(lambda:self.pool.status()['completed']==2)
        self.assertEqual(self.worker.status()['jobs'],{'succeeded':2})
        with self.host.connect() as db:self.assertEqual(db.execute("SELECT COUNT(*) FROM executions WHERE status='succeeded'").fetchone()[0],2)

    def test_failure_is_unknown_and_does_not_replay_or_stop_another_execution(self):
        original=self.handle
        def handler(value):
            if value['id']=='a':self.executed.append('a');raise RuntimeError('synthetic-private-detail')
            return original(value)
        self.registry.handlers[self.registry.active[('example','echo')]]=handler
        for key in ['a','b','c']:self.add(key)
        self.pool.step();self.wait(lambda:self.pool.status()['unknown']==1)
        self.assertTrue(self.entered['b'].wait(2));self.assertNotIn('synthetic-private-detail',str(self.pool.outcomes))
        with self.worker.connect() as db:db.execute("UPDATE jobs SET expires=0 WHERE id='a'")
        self.pool.step();self.assertTrue(self.entered['c'].wait(2))
        self.assertEqual(self.executed.count('a'),1)
        self.assertTrue(self.worker.ingress(fixture('a'))['duplicate'])
        with self.host.connect() as db:self.assertEqual(db.execute("SELECT COUNT(*) FROM executions WHERE status='unknown'").fetchone()[0],1)

    def test_result_response_unknown_does_not_repeat_business_or_publish(self):
        self.add('a');self.gates['a'].set();original=self.client.call
        def call(path,value):
            result=original(path,value)
            if path=='/v1/result':raise OSError('synthetic response lost after commit')
            return result
        self.client.call=call;self.pool.step();self.wait(lambda:self.pool.status()['unknown']==1)
        self.assertEqual(self.worker.status()['jobs'],{'succeeded':1});self.assertEqual(self.pool.step(),0)
        self.assertEqual(self.executed,['a']);self.assertEqual(len([c for c in self.client.calls if c[0]=='/v1/result']),1)

    def test_heartbeat_failure_keeps_only_its_execution_unknown(self):
        for key in ['a','b']:self.add(key)
        original=self.client.call;failed=threading.Event()
        def call(path,value):
            if path=='/v1/heartbeat':
                with self.worker.connect() as db:row=db.execute('SELECT id FROM jobs WHERE execution=?',(value['execution'],)).fetchone()
                if row[0]=='a':failed.set();raise OSError('synthetic heartbeat response lost')
            return original(path,value)
        self.client.call=call;self.pool.step();self.assertTrue(failed.wait(2))
        self.gates['a'].set();self.wait(lambda:self.pool.status()['unknown']==1)
        self.assertFalse(self.gates['b'].is_set());self.gates['b'].set()
        self.wait(lambda:self.pool.status()['completed']==1)
        with self.worker.connect() as db:
            self.assertEqual(dict(db.execute('SELECT id,status FROM jobs')),{'a':'running','b':'succeeded'})

    def test_duplicate_lease_from_dispatch_does_not_start_a_second_owner(self):
        self.add('a');claim=self.worker.claim('pooled-host');original=self.client.call
        self.client.call=lambda path,value:copy.deepcopy(claim) if path=='/v1/claim' else original(path,value)
        with self.assertRaisesRegex(Rejected,'already_dispatched'):self.pool.step()
        self.assertTrue(self.entered['a'].wait(2));self.assertEqual(self.executed,['a'])

    def test_unknown_claim_response_does_not_start_or_requeue_that_lease(self):
        self.add('a');original=self.client.call
        def call(path,value):
            result=original(path,value)
            if path=='/v1/claim' and result:raise OSError('synthetic claim response lost')
            return result
        self.client.call=call
        with self.assertRaises(OSError):self.pool.step()
        self.assertEqual(self.executed,[]);self.assertEqual(self.worker.status()['jobs'],{'running':1})
        with self.worker.connect() as db:db.execute('UPDATE jobs SET expires=0')
        self.client.call=original;self.assertEqual(self.pool.step(),0)
        self.assertEqual(self.worker.status()['jobs'],{'unknown':1})

    def test_close_drains_accepted_leases_with_heartbeats_and_stops_intake(self):
        for key in ['a','b','c']:self.add(key)
        self.pool.step()
        for key in ['a','b']:self.assertTrue(self.entered[key].wait(2))
        closed=threading.Event()
        thread=threading.Thread(target=lambda:(self.pool.close(),closed.set()));thread.start()
        self.wait(lambda:self.pool.status()['closing']);before=len(self.client.calls)
        self.wait(lambda:len(self.client.calls)>before)
        self.assertFalse(closed.is_set());self.assertEqual(self.pool.step(),0)
        for gate in self.gates.values():gate.set()
        thread.join(3);self.assertTrue(closed.is_set())
        self.assertEqual(self.worker.status()['jobs'],{'queued':1,'succeeded':2})

    def test_publication_keeps_running_handlers_and_pins_new_jobs_to_new_version(self):
        for key in ['a','b']:self.add(key)
        self.pool.step()
        for key in ['a','b']:self.assertTrue(self.entered[key].wait(2))
        newer=dict(self.descriptor,snapshot='new-snapshot');incoming=Registry({'schema':1,'entries':[self.descriptor,newer]})
        seen=[]
        incoming.add(self.descriptor,self.handle,contextual=True)
        incoming.add(newer,lambda v:seen.append(v['id']) or self.handle(v),contextual=True)
        self.host.registry=incoming
        self.worker.publish({'generation':1,'declarations':incoming.declarations()});self.add('c')
        self.gates['a'].set();self.wait(lambda:self.pool.status()['completed']==1);self.pool.step()
        self.assertTrue(self.entered['c'].wait(2));self.assertEqual(seen,['c'])
        with self.worker.connect() as db:
            rows=dict(db.execute('SELECT id,binding FROM jobs'))
        import json
        self.assertEqual(json.loads(rows['a'])['snapshot'],'builtin-v1')
        self.assertEqual(json.loads(rows['c'])['snapshot'],'new-snapshot')

    def test_current_revocation_blocks_late_result(self):
        self.add('a');self.pool.step();self.assertTrue(self.entered['a'].wait(2))
        revoked=copy.deepcopy(self.descriptor);revoked['actors']=[dict(fixture()['actor'],user='revoked')]
        incoming=Registry({'schema':1,'entries':[self.descriptor,revoked]});self.host.registry=incoming
        self.gates['a'].set();self.wait(lambda:self.pool.status()['unknown']==1)
        self.assertFalse(any(path=='/v1/result' for path,value in self.client.calls))

    def test_invalid_capacity_and_single_step_dispatch_budget(self):
        for cap in [0,33,True]:
            with self.assertRaises(Rejected):ExecutionPool(self.client,self.host,capacity=cap)
        for i in range(10):self.add(str(i));self.gates[str(i)].set()
        self.assertLessEqual(self.pool.step(),2)
