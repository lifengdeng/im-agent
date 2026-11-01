from pathlib import Path
import tempfile
import unittest

from im_agent.adapters import Registry, echo_descriptor
from im_agent.contracts import Rejected, fixture
from im_agent.ledger import Database
from im_agent.work import WorkItems, install
from im_agent.work_events import Events, install as install_events


class EventTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.db=Database(Path(temp.name)/'events.sqlite3','synthetic-events')
        self.items=WorkItems(Registry());self.allowed=True
        self.events=Events(self.items,lambda who,source,thread:self.allowed)
        self.who=fixture()['actor']
        with self.db.connect() as db:
            install(db);install_events(db)
            self.a=self.items.receive(db,fixture('a'),'thread','a')['item']
            self.b=self.items.receive(db,fixture('b'),'thread','b')['item']

    def source(self, identifier='source', **values):
        return dict({'id':identifier,'actor':self.who,'source':'topic','thread':'source-thread','kind':'update',
            'summary':'untrusted reference','body':'正文'*2500,'links':[]},**values)

    def subscribe(self, db, item, source='topic', thread='source-thread'):
        self.events.subscribe(db,item,self.who,'thread',source,thread)

    def test_explicit_many_to_many_subscriptions_and_immutable_dedup(self):
        with self.db.connect() as db:
            self.subscribe(db,self.a);self.subscribe(db,self.b);self.subscribe(db,self.a,'another')
            r=self.events.receive(db,self.source());self.assertEqual(r['delivered'],2)
            self.assertEqual(self.events.receive(db,self.source())['delivered'],0)
            self.assertEqual(self.events.receive(db,self.source('other',source='another'))['delivered'],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM work_items').fetchone()[0],2)
            with self.assertRaises(Rejected):self.events.receive(db,self.source(body='changed'))

    def test_unsubscribed_events_do_not_wake_or_authorize_execution(self):
        with self.db.connect() as db:
            r=self.events.receive(db,self.source());self.assertEqual(r['delivered'],0)
            with self.assertRaises(Rejected):self.events.read(db,self.a,self.who,'thread',r['id'])
            self.subscribe(db,self.a)
            read=self.events.read(db,self.a,self.who,'thread',r['id'])
            self.assertTrue(read['context_only']);self.assertFalse(read['grants_authority'])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM work_runs').fetchone()[0],0)

    def test_bounded_inline_context_and_paged_spill_read(self):
        with self.db.connect() as db:
            self.subscribe(db,self.a)
            refs=[self.events.receive(db,self.source(str(i)))['id'] for i in range(10)]
            context=self.events.context(db,self.a,self.who,'thread',budget=1024)
            from im_agent.contracts import canonical
            self.assertLessEqual(len(canonical(context['events']).encode()),1024)
            self.assertLessEqual(len(context['events']),8)
            read=self.events.read(db,self.a,self.who,'thread',refs[-1])
            second=self.events.read(db,self.a,self.who,'thread',refs[-1],offset=read['next_offset'])
            self.assertEqual(read['body']+second['body'],('正文'*4000)[:4000])
            for options in [{'offset':True},{'limit':2001},{'offset':-1}]:
                with self.assertRaises(Rejected):self.events.read(db,self.a,self.who,'thread',refs[-1],**options)

    def test_every_read_rechecks_source_and_work_permissions(self):
        with self.db.connect() as db:
            self.subscribe(db,self.a);r=self.events.receive(db,self.source())
            self.allowed=False
            with self.assertRaises(Rejected):self.events.read(db,self.a,self.who,'thread',r['id'])
            self.allowed=True
            self.events.unsubscribe(db,self.a,self.who,'thread','topic','source-thread')
            with self.assertRaises(Rejected):self.events.read(db,self.a,self.who,'thread',r['id'])
            self.subscribe(db,self.a)
            d=echo_descriptor();d['actors']=[dict(self.who,user='other')];self.items.registry=Registry({'schema':1,'entries':[d]})
            with self.assertRaises(Rejected):self.events.context(db,self.a,self.who,'thread')

    def test_full_actor_and_thread_boundaries_and_link_authorization(self):
        with self.db.connect() as db:
            self.subscribe(db,self.a)
            hidden=self.events.receive(db,self.source('hidden',thread='not-subscribed'))['id']
            visible=self.events.receive(db,self.source('visible',links=[hidden]))['id']
            self.assertEqual(self.events.read(db,self.a,self.who,'thread',visible)['links'],[])
            for key in self.who:
                who=dict(self.who,**{key:'private' if key=='kind' else 'foreign'})
                with self.assertRaises(Rejected):self.events.read(db,self.a,who,'thread',visible)
            with self.assertRaises(Rejected):self.events.read(db,self.a,self.who,'foreign',visible)
            self.subscribe(db,self.a,thread='not-subscribed')
            self.assertEqual(self.events.read(db,self.a,self.who,'thread',visible)['links'][0]['reference'],hidden)

    def test_payload_and_routing_tamper_fail_closed(self):
        with self.db.connect() as db:
            self.subscribe(db,self.a);r=self.events.receive(db,self.source())
            db.execute("UPDATE work_source_events SET payload='{}' WHERE id=?",(r['id'],))
            with self.assertRaises(Rejected):self.events.read(db,self.a,self.who,'thread',r['id'])
