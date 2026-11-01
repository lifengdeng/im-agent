"""Scoped work coordination in a worker-owned transaction.

Execution and delivery ledgers remain authoritative. Callers provide trusted
registrations, thread keys and job references; model output grants no authority.
This module has no channel, model, project or process dependencies.
"""
import json
import time

from .contracts import Rejected, actor, canonical, event, fingerprint, identifier
from .adapters import Registry


def install(db):
    """Additive tables; the caller owns the database and its transaction."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS work_items (
            id TEXT PRIMARY KEY, actor TEXT NOT NULL, thread TEXT NOT NULL,
            origin TEXT NOT NULL, binding TEXT NOT NULL, job TEXT UNIQUE NOT NULL,
            state TEXT NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS work_inputs (
            id TEXT PRIMARY KEY, digest TEXT NOT NULL, item TEXT,
            kind TEXT NOT NULL, decision TEXT NOT NULL, receipt TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS work_runs (
            id TEXT PRIMARY KEY, item TEXT NOT NULL, kind TEXT NOT NULL,
            binding TEXT NOT NULL, state TEXT NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS work_grants (
            contract TEXT PRIMARY KEY, descriptor TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS work_links (
            item TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL,
            binding TEXT NOT NULL, PRIMARY KEY(item,kind,id));
        CREATE INDEX IF NOT EXISTS work_scope ON work_items(actor,thread,state);
        CREATE INDEX IF NOT EXISTS work_input_item ON work_inputs(item);
    """)


class WorkItems:
    def __init__(self, registry):
        self.registry = registry

    def get(self, db, item, who, thread):
        actor(who); identifier(item); identifier(thread)
        row = db.execute("SELECT * FROM work_items WHERE id=? AND actor=? AND thread=?",
                         (item, canonical(who), thread)).fetchone()
        if row is None:
            raise Rejected("work_item_unavailable")
        grant = json.loads(row['binding'])
        origin = json.loads(row['origin'])
        # Current policy is checked before loading a historical contract. A
        # new registration's actor list may change without revoking this actor.
        self.registry.descriptor(origin)
        saved = db.execute('SELECT descriptor FROM work_grants WHERE contract=?', (grant['contract'],)).fetchone()
        if saved is None: raise Rejected('work_grant_unavailable')
        historical = Registry({'schema': 1, 'entries': [json.loads(saved[0])]})
        if grant['contract'] not in historical.versions: raise Rejected('work_grant_binding_changed')
        self.registry.versions[grant['contract']] = historical.versions[grant['contract']]
        contract, selected = self.registry.descriptor(origin, {'contract': grant['contract']})
        if Registry._binding(contract, selected) != grant['execution']:
            raise Rejected('work_grant_binding_changed')
        return dict(row)

    def receive(self, db, value, thread, job, *, kind="request", reference=None):
        """Record a routing decision once, in the same transaction as intake.

        Only explicit continuation is routed to existing work. A status/stop
        without a reference requires exactly one live item in this full scope.
        Completed and uncertain work is never automatically reopened.
        """
        event(value, self.registry); identifier(thread); identifier(job)
        if kind not in {"request", "continue", "status", "stop"}:
            raise Rejected("work_input_kind_invalid")
        if reference is not None:
            identifier(reference)
        if kind == "request" and reference is not None:
            raise Rejected("work_request_reference_invalid")
        digest = fingerprint([value, thread, job, kind, reference])
        prior = db.execute("SELECT * FROM work_inputs WHERE id=?", (value["id"],)).fetchone()
        if prior:
            if prior["digest"] != digest:
                raise Rejected("work_input_collision")
            if prior["item"]:
                self.get(db, prior["item"], value["actor"], thread)
            return dict(json.loads(prior["decision"]), duplicate=True)
        item = None
        if kind == "request":
            item = fingerprint(["work-item-v1", value["actor"], value["id"]])
            binding = {'contract': self.registry.descriptor(value)[0], 'execution': self.registry.binding(value)}
            db.execute('INSERT OR IGNORE INTO work_grants VALUES(?,?)',
                       (binding['contract'], canonical(self.registry.versions[binding['contract']])))
            db.execute("INSERT INTO work_items VALUES(?,?,?,?,?,?, 'queued',?)",
                       (item, canonical(value["actor"]), thread, canonical(value), canonical(binding), job, time.time()))
            decision = {"action": "new", "item": item, "job": job}
        else:
            if reference is not None:
                rows = db.execute("""SELECT DISTINCT w.id FROM work_items w
                    LEFT JOIN work_inputs i ON i.item=w.id
                    LEFT JOIN work_links l ON l.item=w.id AND l.kind='delivery'
                    WHERE w.actor=? AND w.thread=? AND (w.id=? OR i.id=? OR l.id=?)""",
                    (canonical(value["actor"]), thread, reference, reference, reference)).fetchall()
            else:
                rows = db.execute("""SELECT id FROM work_items WHERE actor=? AND thread=?
                    AND state IN ('queued','running','waiting','stopping','unknown')""",
                    (canonical(value["actor"]), thread)).fetchall()
            decision = {"action": "clarify", "reason": "work_reference_required"}
            if len(rows) == 1:
                row = self.get(db, rows[0]["id"], value["actor"], thread)
                origin = json.loads(row['origin'])
                if (origin['project'], origin['capability']) != (value['project'], value['capability']):
                    raise Rejected('work_capability_changed')
                item = row["id"]
                decision = {"action": kind, "item": item, "job": row["job"]}
                if kind != "status" and row["state"] in {"unknown", "stopping"}:
                    decision.update(action="reconcile", reason="work_result_unknown")
                elif kind != "status" and row["state"] not in {"queued", "running", "waiting"}:
                    decision.update(action="clarify", reason="work_already_ended")
        db.execute("INSERT INTO work_inputs VALUES(?,?,?,?,?,?)",
                   (value["id"], digest, item, kind, canonical(decision), canonical({"status": "received"})))
        return dict(decision, duplicate=False)

    @staticmethod
    def bind_run(db, item, run, kind, binding):
        """An immutable reference to an accepted execution, never a new attempt."""
        identifier(item); identifier(run)
        if kind not in {"conversation", "native"} or not isinstance(binding, dict) or not binding:
            raise Rejected("work_run_invalid")
        owner = db.execute("SELECT state FROM work_items WHERE id=?", (item,)).fetchone()
        if owner is None:
            raise Rejected("work_item_unavailable")
        prior = db.execute("SELECT * FROM work_runs WHERE id=?", (run,)).fetchone()
        if prior:
            if (prior["item"], prior["kind"], prior["binding"]) != (item, kind, canonical(binding)):
                raise Rejected("work_run_binding_changed")
            return
        if owner['state'] not in {'queued', 'running', 'waiting'}:
            raise Rejected('work_run_requires_reconciliation')
        db.execute("INSERT INTO work_runs VALUES(?,?,?,?, 'queued',?)",
                   (run, item, kind, canonical(binding), time.time()))

    @staticmethod
    def bind_reference(db, item, kind, reference, binding):
        """Trusted, verified external references, never model-selected paths."""
        identifier(item); identifier(reference)
        if kind not in {'native-task', 'native-thread', 'delivery'} or not isinstance(binding, dict) or not binding:
            raise Rejected('work_reference_invalid')
        if db.execute('SELECT 1 FROM work_items WHERE id=?', (item,)).fetchone() is None:
            raise Rejected('work_item_unavailable')
        prior = db.execute('SELECT binding FROM work_links WHERE item=? AND kind=? AND id=?', (item, kind, reference)).fetchone()
        if prior:
            if prior[0] != canonical(binding): raise Rejected('work_reference_binding_changed')
            return
        if db.execute('SELECT COUNT(*) FROM work_links WHERE item=?', (item,)).fetchone()[0] >= 64:
            raise Rejected('work_reference_budget')
        db.execute('INSERT INTO work_links VALUES(?,?,?,?)', (item, kind, reference, canonical(binding)))

    @staticmethod
    def observe(db, item, state):
        if state not in {"queued", "running", "waiting", "stopping", "completed", "failed", "stopped", "unknown"}:
            raise Rejected("work_state_invalid")
        db.execute("UPDATE work_items SET state=?,updated=? WHERE id=?", (state, time.time(), item))

    @staticmethod
    def receipt(db, input_id, status):
        if status not in {"received", "queued", "sending", "accepted", "rejected", "unknown"}:
            raise Rejected("work_receipt_invalid")
        prior = db.execute("SELECT receipt FROM work_inputs WHERE id=?", (input_id,)).fetchone()
        if prior is None:
            raise Rejected("work_input_unavailable")
        previous = json.loads(prior[0])["status"]
        if previous == status: return
        if previous in {"accepted", "rejected", "unknown"} and status != previous:
            raise Rejected("work_receipt_requires_reconciliation")
        db.execute("UPDATE work_inputs SET receipt=? WHERE id=?", (canonical({"status": status}), input_id))

    @staticmethod
    def reconcile_receipt(db, input_id, status, proof):
        """Administrative bookkeeping after an adapter has verified evidence."""
        if status not in {'accepted', 'rejected'} or not isinstance(proof, str) or len(proof) != 64 or any(c not in '0123456789abcdef' for c in proof):
            raise Rejected('work_reconciliation_proof_required')
        row = db.execute('SELECT receipt FROM work_inputs WHERE id=?', (input_id,)).fetchone()
        previous = json.loads(row[0]) if row else None
        receipt = {'status': status, 'reconciliation': proof}
        if previous == receipt: return
        if previous != {'status': 'unknown'}: raise Rejected('work_reconciliation_compare_and_set_failed')
        db.execute('UPDATE work_inputs SET receipt=? WHERE id=?', (canonical(receipt), input_id))

    def view(self, db, item, who, thread):
        row = self.get(db, item, who, thread)
        runs = [dict(r) for r in db.execute("SELECT id,kind,binding,state FROM work_runs WHERE item=? ORDER BY rowid", (item,))]
        for run in runs:
            run["binding"] = json.loads(run["binding"])
        return {"id": item, "state": row["state"], "job": row["job"], "runs": runs,
                "references": [dict(kind=r['kind'], id=r['id'], binding=json.loads(r['binding'])) for r in db.execute(
                    'SELECT kind,id,binding FROM work_links WHERE item=? ORDER BY rowid', (item,))],
                "inputs": [dict(id=r["id"], kind=r["kind"], **json.loads(r["receipt"])) for r in db.execute(
                    """SELECT id,kind,receipt FROM (SELECT rowid,id,kind,receipt FROM work_inputs
                        WHERE item=? ORDER BY rowid DESC LIMIT 64) ORDER BY rowid""", (item,))]}


class Pending:
    """Trusted asynchronous boundary; poll returns None until a result exists."""
    def poll(self):
        raise NotImplementedError

    def wait(self):
        while True:
            value = self.poll()
            if value is not None:
                return value
            time.sleep(0.1)
