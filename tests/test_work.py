import json
from pathlib import Path
import tempfile
import unittest

from im_agent.adapters import Registry, echo_descriptor
from im_agent.contracts import Rejected, fixture
from im_agent.ledger import Database, Worker
from im_agent.work import WorkItems, install


class WorkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.db = Database(Path(temporary.name) / 'worker.sqlite3', 'synthetic-worker')
        self.registry = Registry()
        self.work = WorkItems(self.registry)
        with self.db.connect() as db: install(db)
        self.origin = fixture('first')
        self.first = self.receive(self.origin)

    def receive(self, value, **options):
        with self.db.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            return self.work.receive(db, value, 'thread-a', value['id'], **options)

    def test_scope_and_thread_are_checked_before_context_is_returned(self):
        with self.db.connect() as db:
            for field in self.origin['actor']:
                who = dict(self.origin['actor'], **{field: 'private' if field == 'kind' else 'other'})
                with self.assertRaises(Rejected): self.work.view(db, self.first['item'], who, 'thread-a')
            with self.assertRaises(Rejected): self.work.view(db, self.first['item'], self.origin['actor'], 'thread-b')

    def test_new_work_and_explicit_continuation_have_distinct_input_ids(self):
        followup = self.receive(fixture('followup'), kind='continue', reference='first')
        self.assertEqual(followup['item'], self.first['item'])
        self.assertEqual(followup['job'], 'first')
        self.assertEqual(self.receive(fixture('new'))['action'], 'new')
        ambiguous = self.receive(fixture('status'), kind='status')
        self.assertEqual(ambiguous['action'], 'clarify')

    def test_ambiguous_decision_remains_idempotent_when_work_changes(self):
        self.receive(fixture('second'))
        value = fixture('ambiguous')
        first = self.receive(value, kind='continue')
        with self.db.connect() as db: self.work.observe(db, self.first['item'], 'completed')
        self.assertEqual(self.receive(value, kind='continue'), dict(first, duplicate=True))

    def test_repeat_is_deduplicated_and_changed_payload_or_target_is_rejected(self):
        value = fixture('followup')
        accepted = self.receive(value, kind='continue', reference='first')
        self.assertEqual(self.receive(value, kind='continue', reference='first'), dict(accepted, duplicate=True))
        with self.assertRaises(Rejected): self.receive(value, kind='stop', reference='first')
        value['input']['text'] = 'changed'
        with self.assertRaises(Rejected): self.receive(value, kind='continue', reference='first')

    def test_missing_reference_cannot_reveal_other_scope_or_start_work(self):
        value = fixture('foreign')
        d = echo_descriptor(); d['actors'].append(dict(value['actor'], user='bob'))
        self.work.registry = Registry({'schema': 1, 'entries': [d]})
        value['actor']['user'] = 'bob'
        decision = self.receive(value, kind='continue', reference=self.first['item'])
        self.assertEqual(decision['action'], 'clarify')
        self.assertNotIn('item', decision)

    def test_current_revocation_blocks_even_pinned_and_duplicate_requests(self):
        d = echo_descriptor(); d['actors'] = [dict(self.origin['actor'], user='bob')]
        self.work.registry = Registry({'schema': 1, 'entries': [d]})
        with self.assertRaises(Rejected): self.receive(self.origin)
        with self.db.connect() as db:
            with self.assertRaises(Rejected): self.work.get(db, self.first['item'], self.origin['actor'], 'thread-a')

    def test_capability_change_cannot_be_appended_to_another_grant(self):
        d = echo_descriptor(); d['capability'] = 'other'
        self.registry.add(d)
        value = fixture('different-capability'); value['capability'] = 'other'
        with self.assertRaisesRegex(Rejected, 'work_capability_changed'):
            self.receive(value, kind='continue', reference='first')

    def test_unknown_and_completed_work_never_reopens_automatically(self):
        for state, action in [('unknown', 'reconcile'), ('stopping', 'reconcile'), ('completed', 'clarify')]:
            with self.db.connect() as db: self.work.observe(db, self.first['item'], state)
            decision = self.receive(fixture('follow-'+state), kind='continue', reference='first')
            self.assertEqual(decision['action'], action)
            self.assertEqual(self.receive(fixture('status-'+state), kind='status', reference='first')['action'], 'status')

    def test_run_binding_is_immutable_and_new_attempt_cannot_escape_unknown(self):
        with self.db.connect() as db:
            self.work.bind_run(db, self.first['item'], 'native-request', 'native', {'snapshot': 'old'})
            self.work.observe(db, self.first['item'], 'unknown')
            self.work.bind_run(db, self.first['item'], 'native-request', 'native', {'snapshot': 'old'})
            with self.assertRaises(Rejected): self.work.bind_run(db, self.first['item'], 'native-request', 'native', {'snapshot': 'new'})
            with self.assertRaises(Rejected): self.work.bind_run(db, self.first['item'], 'another-attempt', 'native', {'snapshot': 'new'})

    def test_receipt_does_not_turn_acceptance_into_execution_or_delivery_success(self):
        value = fixture('stop')
        self.receive(value, kind='stop', reference='first')
        with self.db.connect() as db:
            self.work.receipt(db, value['id'], 'queued')
            self.work.receipt(db, value['id'], 'sending')
            self.work.receipt(db, value['id'], 'unknown')
            with self.assertRaises(Rejected): self.work.receipt(db, value['id'], 'accepted')
            view = self.work.view(db, self.first['item'], self.origin['actor'], 'thread-a')
            self.assertEqual(view['state'], 'queued')
            self.assertEqual(view['inputs'][-1]['status'], 'unknown')
            self.work.reconcile_receipt(db, value['id'], 'accepted', 'a'*64)
            self.work.reconcile_receipt(db, value['id'], 'accepted', 'a'*64)
            self.work.receipt(db, value['id'], 'accepted')
            self.assertEqual(json.loads(db.execute('SELECT receipt FROM work_inputs WHERE id=?', (value['id'],)).fetchone()[0])['reconciliation'], 'a'*64)
            with self.assertRaises(Rejected): self.work.reconcile_receipt(db, value['id'], 'accepted', 'b'*64)

    def test_registration_update_keeps_original_snapshot(self):
        d = echo_descriptor(); d['snapshot'] = 'new'
        old_contract = self.registry.active[('example', 'echo')]
        self.registry.add(d)
        followup = self.receive(fixture('later'), kind='continue', reference='first')
        with self.db.connect() as db:
            row = self.work.get(db, followup['item'], self.origin['actor'], 'thread-a')
        self.assertEqual(json.loads(row['binding']), {'contract': old_contract, 'execution': Registry._binding(old_contract, echo_descriptor())})

    def test_rebuilt_registry_and_expanded_actor_list_preserve_authorized_old_work(self):
        d = echo_descriptor(); d['actors'].append(dict(self.origin['actor'], user='bob'))
        self.work = WorkItems(Registry({'schema': 1, 'entries': [d]}))
        self.assertEqual(self.receive(fixture('after-restart'), kind='continue', reference='first')['item'], self.first['item'])

    def test_verified_reference_is_scoped_immutable_and_does_not_grant_execution(self):
        with self.db.connect() as db:
            self.work.bind_reference(db, self.first['item'], 'delivery', 'platform-reply', {'receipt': 'verified-hash'})
            with self.assertRaises(Rejected): self.work.bind_reference(db, self.first['item'], 'delivery', 'platform-reply', {'receipt': 'changed'})
            with self.assertRaises(Rejected): self.work.bind_reference(db, self.first['item'], 'native-task', '/arbitrary/path', {'receipt': 'hash'})
        result = self.receive(fixture('reply-status'), kind='status', reference='platform-reply')
        self.assertEqual(result['item'], self.first['item'])

    def test_rollback_of_intake_transaction_leaves_no_half_work_or_input(self):
        try:
            with self.db.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                self.work.receive(db, fixture('aborted'), 'thread-a', 'aborted')
                raise RuntimeError('synthetic crash')
        except RuntimeError: pass
        with self.db.connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM work_inputs WHERE id='aborted'").fetchone())

    def test_internal_cancel_is_atomic_scoped_and_cannot_cancel_a_claimed_execution(self):
        worker = Worker(self.db.path.parent / 'execution.sqlite3')
        worker.submit_internal(fixture('cancel-before-start'), 'parent-a')
        with self.assertRaises(Rejected): worker.cancel_queued('cancel-before-start', 'other-parent')
        self.assertTrue(worker.cancel_queued('cancel-before-start', 'parent-a')['cancelled'])
        self.assertTrue(worker.cancel_queued('cancel-before-start', 'parent-a')['cancelled'])
        self.assertIsNone(worker.claim('host'))
        worker.submit_internal(fixture('running'), 'parent-a')
        claim = worker.claim('host', now=1)
        self.assertFalse(worker.cancel_queued('running', 'parent-a')['cancelled'])
        worker.claim('host', now=62)
        self.assertFalse(worker.cancel_queued('running', 'parent-a')['cancelled'])
        self.assertEqual(worker.internal('running', 'parent-a')['status'], 'unknown')

    def test_claim_guard_preserves_blocked_work_and_does_not_starve_other_parents(self):
        worker = Worker(self.db.path.parent / 'guarded.sqlite3', claim_guard=lambda value, parent: parent == 'active')
        for number in range(70): worker.submit_internal(fixture('held-'+str(number)), 'uncertain')
        worker.submit_internal(fixture('eligible'), 'active')
        self.assertEqual(worker.claim('host')['event']['id'], 'eligible')
        self.assertIsNone(worker.claim('host'))
        self.assertEqual(worker.internal('held-0', 'uncertain')['status'], 'queued')


if __name__ == '__main__': unittest.main()
