"""Component-owned SQLite ledgers; no shared DB between containers and host."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
import threading
import uuid

from .contracts import Rejected, canonical, envelope, event, fingerprint, identifier
from .adapters import Registry


class Database:
    def __init__(self, path, owner):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS ownership (owner TEXT PRIMARY KEY)")
            previous = db.execute("SELECT owner FROM ownership").fetchall()
            if previous and [r[0] for r in previous] != [owner]:
                raise Rejected("database_owner_mismatch")
            db.execute("INSERT OR IGNORE INTO ownership VALUES(?)", (owner,))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()


class Worker(Database):
    def __init__(self, path, registry=None, *, receipt_kind="offline"):
        self.registry = registry or Registry()
        self.registration_lock = threading.RLock()
        super().__init__(path, "worker-v1")
        from .delivery import bind_receipts
        self.receipt_kind = bind_receipts(self, receipt_kind)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, digest TEXT NOT NULL, payload TEXT NOT NULL,
                    status TEXT NOT NULL, execution TEXT UNIQUE, executor TEXT, lease TEXT,
                    expires REAL, binding TEXT, result TEXT, delivered INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS events (
                    execution TEXT NOT NULL, seq INTEGER NOT NULL, digest TEXT NOT NULL,
                    PRIMARY KEY(execution,seq));
            """)
            if "parent" not in {r[1] for r in db.execute("PRAGMA table_info(jobs)")}:
                db.execute("ALTER TABLE jobs ADD COLUMN parent TEXT")
            db.executescript("""CREATE TABLE IF NOT EXISTS registrations(contract TEXT PRIMARY KEY,descriptor TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS registry_state(singleton INTEGER PRIMARY KEY CHECK(singleton=1),generation INTEGER,payload TEXT NOT NULL);""")
            saved = db.execute("SELECT * FROM registry_state").fetchone()
            if saved:
                active = Registry(json.loads(saved["payload"]))
                restored = Registry(active.declarations())
                for row in db.execute("SELECT descriptor FROM registrations ORDER BY contract"): restored.add(json.loads(row[0]))
                # Initial trusted declarations are the authority ceiling, even after restart.
                self._check_policies(active)
                restored.active = active.active
                self.registry = restored
            else:
                for contract, d in self.registry.versions.items():
                    db.execute("INSERT INTO registrations VALUES(?,?)", (contract, canonical(d)))
                db.execute("INSERT INTO registry_state VALUES(1,0,?)", (canonical(self.registry.declarations()),))

    def _check_policies(self, proposed):
        if set(proposed.active) != set(self.registry.active): raise Rejected("registered_capability_set_changed")
        for key, contract in proposed.active.items():
            if Registry.policy(proposed.versions[contract]) != Registry.policy(self.registry.versions[self.registry.active[key]]):
                raise Rejected("registered_authority_changed")

    def registration(self):
        with self.connect() as db:
            row = db.execute("SELECT * FROM registry_state").fetchone()
        return {"generation": row["generation"], "declarations": json.loads(row["payload"])}

    def publish(self, value):
        if set(value) != {"generation", "declarations"} or type(value["generation"]) is not int or value["generation"] < 1:
            raise Rejected("registry_publication_invalid")
        proposed = Registry(value["declarations"])
        payload = canonical(proposed.declarations())
        with self.registration_lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_policies(proposed)
            old = db.execute("SELECT * FROM registry_state").fetchone()
            if value["generation"] < old["generation"] or value["generation"] == old["generation"] and payload != old["payload"]:
                raise Rejected("registry_generation_conflict")
            combined = Registry(self.registry.declarations())
            for d in self.registry.versions.values(): combined.add(d)
            for d in proposed.versions.values():
                contract = combined.add(d)
                db.execute("INSERT OR IGNORE INTO registrations VALUES(?,?)", (contract, canonical(d)))
            combined.active = proposed.active
            db.execute("UPDATE registry_state SET generation=?,payload=?", (value["generation"], payload))
            self.registry = combined
        return self.registration()

    def ingress(self, value):
        return self._ingress(value, None)

    def submit_internal(self, value, parent):
        identifier(parent)
        return self._ingress(value, parent)

    def internal(self, job, parent):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=? AND parent=?", (job, parent)).fetchone()
        if not row: raise Rejected("internal_job_missing")
        return {"status": row["status"], "binding": json.loads(row["binding"]),
                "result": json.loads(row["result"]) if row["result"] else None}

    def _ingress(self, value, parent):
        envelope(value)
        digest = fingerprint(value)
        with self.registration_lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT digest,binding,parent FROM jobs WHERE id=?", (value["id"],)).fetchone()
            if prior:
                if prior[0] != digest or prior["parent"] != parent:
                    raise Rejected("ingress_id_collision")
                self.registry.verify_binding(value, json.loads(prior["binding"]) if prior["binding"] else self.registry.binding(value))
                return {"job": value["id"], "duplicate": True}
            event(value, self.registry)
            db.execute("INSERT INTO jobs(id,digest,payload,status,binding,parent) VALUES(?,?,?,'queued',?,?)",
                       (value["id"], digest, canonical(value), canonical(self.registry.binding(value)), parent))
        return {"job": value["id"], "duplicate": False}

    def claim(self, executor, now=None):
        identifier(executor)
        now = time.time() if now is None else now
        with self.registration_lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # Expiry means execution may have happened. Never turn it into queued.
            db.execute("UPDATE jobs SET status='unknown' WHERE status='running' AND expires<?", (now,))
            row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
            if not row:
                return None
            execution, lease = uuid.uuid4().hex, uuid.uuid4().hex
            payload = json.loads(row["payload"])
            binding = json.loads(row["binding"]) if row["binding"] else self.registry.binding(payload)
            self.registry.verify_binding(payload, binding)
            db.execute("UPDATE jobs SET status='running',execution=?,executor=?,lease=?,expires=?,binding=? WHERE id=?",
                       (execution, executor, lease, now + 60, canonical(binding), row["id"]))
            return {"execution": execution, "executor": executor, "lease": lease, "binding": binding,
                    "event": json.loads(row["payload"])}

    @staticmethod
    def checked(db, value, now=None):
        row = db.execute("SELECT * FROM jobs WHERE execution=?", (value["execution"],)).fetchone()
        if (not row or row["executor"] != value["executor"] or row["lease"] != value["lease"]
                or json.loads(row["binding"]) != value["binding"]):
            raise Rejected("execution_binding_rejected")
        if row["status"] == "running" and row["expires"] < (time.time() if now is None else now):
            raise Rejected("lease_expired_unknown")
        return row

    def progress(self, value):
        if set(value) != {"execution", "executor", "lease", "binding", "seq", "phase"}:
            raise Rejected("progress_invalid")
        if type(value["seq"]) is not int or value["seq"] < 1 or value["phase"] not in {"started", "verified"}:
            raise Rejected("progress_invalid")
        digest = fingerprint(value)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.checked(db, value)
            prior = db.execute("SELECT digest FROM events WHERE execution=? AND seq=?",
                               (value["execution"], value["seq"])).fetchone()
            if prior:
                if prior[0] != digest:
                    raise Rejected("progress_collision")
                return {"duplicate": True}
            seq = db.execute("SELECT COALESCE(MAX(seq),0) FROM events WHERE execution=?", (value["execution"],)).fetchone()[0]
            if value["seq"] != seq + 1 or row["status"] != "running":
                raise Rejected("progress_order_rejected")
            db.execute("INSERT INTO events VALUES(?,?,?)", (value["execution"], value["seq"], digest))
        return {"duplicate": False}

    def heartbeat(self, value):
        if set(value) != {"execution", "executor", "lease", "binding"}: raise Rejected("heartbeat_invalid")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.checked(db, value)
            if row["status"] != "running": raise Rejected("heartbeat_execution_closed")
            db.execute("UPDATE jobs SET expires=? WHERE id=?", (time.time() + 60, row["id"]))
        return {"accepted": True}

    def finish(self, value):
        if set(value) != {"execution", "executor", "lease", "binding", "result"}:
            raise Rejected("result_invalid")
        result = value["result"]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.checked(db, value)
            original = json.loads(row["payload"])
            self.registry.result(original, json.loads(row["binding"]), result)
            if row["status"] == "succeeded":
                if row["result"] != canonical(result):
                    raise Rejected("result_collision")
                return {"duplicate": True}
            if row["status"] != "running":
                raise Rejected("unknown_execution_requires_reconciliation")
            db.execute("UPDATE jobs SET status='succeeded',result=? WHERE id=?", (canonical(result), row["id"]))
        return {"duplicate": False}

    def delivery_intents(self):
        with self.connect() as db:
            rows = db.execute("SELECT id,payload,result FROM jobs WHERE status='succeeded' AND delivered=0 AND parent IS NULL").fetchall()
        return [{"id": r["id"], "actor": json.loads(r["payload"])["actor"], "result": json.loads(r["result"])} for r in rows]

    def receipt(self, value):
        if set(value) != {"id", "digest", "platform_id", "verified"} or value["verified"] is not True:
            raise Rejected("delivery_receipt_invalid")
        from .delivery import validate_platform_id
        validate_platform_id(self.receipt_kind, value["id"], value["platform_id"])
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=? AND status='succeeded' AND parent IS NULL", (value["id"],)).fetchone()
            if not row:
                raise Rejected("delivery_job_missing")
            intent = {"id": row["id"], "actor": json.loads(row["payload"])["actor"], "result": json.loads(row["result"])}
            if fingerprint(intent) != value["digest"]:
                raise Rejected("delivery_receipt_mismatch")
            db.execute("UPDATE jobs SET delivered=1 WHERE id=?", (value["id"],))
        return {"accepted": True}

    def status(self):
        with self.connect() as db:
            states = dict(db.execute("SELECT status,COUNT(*) FROM jobs GROUP BY status").fetchall())
            delivered = db.execute("SELECT COUNT(*) FROM jobs WHERE delivered=1").fetchone()[0]
        return {"protocol": 1, "mode": "offline", "jobs": states, "delivered": delivered,
                "production_enabled": False, "model_calls": 0}


class Transport(Database):
    def __init__(self, path, registry=None, *, receipt_kind="offline"):
        self.registry = registry or Registry()
        super().__init__(path, "transport-v1")
        from .delivery import bind_receipts
        self.receipt_kind = bind_receipts(self, receipt_kind)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS inbox(id TEXT PRIMARY KEY,digest TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,digest TEXT NOT NULL,status TEXT NOT NULL,receipt TEXT)")
            db.execute("UPDATE outbox SET status='unknown' WHERE status='sending'")

    def receive(self, value):
        event(value, self.registry)
        with self.connect() as db:
            prior = db.execute("SELECT digest FROM inbox WHERE id=?", (value["id"],)).fetchone()
            if prior and prior[0] != fingerprint(value):
                raise Rejected("transport_id_collision")
            db.execute("INSERT OR IGNORE INTO inbox VALUES(?,?)", (value["id"], fingerprint(value)))

    def deliver(self, intent, sink):
        digest = fingerprint(intent)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM outbox WHERE id=?", (intent["id"],)).fetchone()
            if prior:
                if prior["digest"] != digest:
                    raise Rejected("delivery_intent_collision")
                if prior["status"] == "verified":
                    return json.loads(prior["receipt"])
                raise Rejected("unknown_delivery_requires_reconciliation")
            db.execute("INSERT INTO outbox VALUES(?,?,'sending',NULL)", (intent["id"], digest))
        try:
            platform_id = sink(intent)
            receipt = {"id": intent["id"], "digest": digest, "platform_id": platform_id, "verified": True}
            from .delivery import validate_platform_id
            validate_platform_id(self.receipt_kind, intent["id"], platform_id, sink=True)
        except Exception:
            with self.connect() as db:
                db.execute("UPDATE outbox SET status='unknown' WHERE id=?", (intent["id"],))
            raise
        with self.connect() as db:
            db.execute("UPDATE outbox SET status='verified',receipt=? WHERE id=?", (canonical(receipt), intent["id"]))
        return receipt


class Host(Database):
    def __init__(self, path, registry=None):
        self.registry = registry or Registry()
        super().__init__(path, "host-v1")
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS executions(id TEXT PRIMARY KEY,digest TEXT NOT NULL,status TEXT NOT NULL,result TEXT)")
            db.execute("UPDATE executions SET status='unknown' WHERE status='running'")

    def execute(self, claim, adapter=None):
        value = envelope(claim["event"])
        self.registry.verify_binding(value, claim["binding"])
        digest = fingerprint(claim)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM executions WHERE id=?", (claim["execution"],)).fetchone()
            if row:
                if row["digest"] != digest:
                    raise Rejected("host_claim_collision")
                if row["status"] == "succeeded":
                    return json.loads(row["result"])
                raise Rejected("host_unknown_requires_reconciliation")
            db.execute("INSERT INTO executions VALUES(?,?,'running',NULL)", (claim["execution"], digest))
        try:
            result = self.registry.execute(value, claim["binding"], adapter)
        except Exception:
            with self.connect() as db:
                db.execute("UPDATE executions SET status='unknown' WHERE id=?", (claim["execution"],))
            raise
        with self.connect() as db:
            db.execute("UPDATE executions SET status='succeeded',result=? WHERE id=?", (canonical(result), claim["execution"]))
        return result
