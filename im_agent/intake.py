"""Durable bounded intake, extracted from instance spool/forwarding behavior."""
import json
import threading

from .contracts import Rejected, canonical, envelope, fingerprint
from .ledger import Database


class Inbox(Database):
    def __init__(self, path, registry, *, recover_planning=False, capabilities=("inspect-release", "help")):
        self.registry = registry
        self.capabilities = frozenset(capabilities)
        self.lock = threading.Lock()
        super().__init__(path, "twin-intake-v1")
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS intake(id TEXT PRIMARY KEY,digest TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL,planned TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS model_attempts(id TEXT PRIMARY KEY,status TEXT NOT NULL,receipt TEXT)")
            if recover_planning:
                db.execute("UPDATE model_attempts SET status='unknown' WHERE status='started' AND id IN (SELECT id FROM intake WHERE status='planning')")
                db.execute("UPDATE intake SET status='unknown' WHERE status='planning'")

    def accept(self, value):
        envelope(value)
        self.registry.binding(value)
        digest = fingerprint(value)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT digest FROM intake WHERE id=?", (value["id"],)).fetchone()
            if prior:
                if prior[0] != digest:
                    raise Rejected("durable_intake_collision")
                return {"duplicate": True}
            if db.execute("SELECT COUNT(*) FROM intake WHERE status!='submitted'").fetchone()[0] >= 64:
                raise Rejected("durable_intake_full")
            db.execute("INSERT INTO intake VALUES(?,?,?,'queued',NULL)", (value["id"], digest, canonical(value)))
        return {"duplicate": False}

    def forward(self, send):
        with self.connect() as db:
            rows = db.execute("SELECT id,payload FROM intake WHERE status='queued' ORDER BY rowid LIMIT 16").fetchall()
        for row in rows:
            send(json.loads(row["payload"]))
            with self.connect() as db:
                db.execute("UPDATE intake SET status='submitted' WHERE id=?", (row["id"],))
        return len(rows)

    def plan(self, planner, submit):
        # One model attempt per accepted request. A process loss during a model
        # call becomes unknown; retry only the already-persisted idempotent ingress.
        with self.lock:
            external = getattr(planner, "provider", None) == "deepseek"
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM intake WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
                if row:
                    db.execute("UPDATE intake SET status='planning' WHERE id=?", (row["id"],))
                    if external:
                        db.execute("INSERT INTO model_attempts VALUES(?,'started',NULL)", (row["id"],))
            if row:
                try:
                    value = json.loads(row["payload"])
                    capability = planner.choose(value["input"]["text"])
                    if capability not in self.capabilities:
                        raise Rejected("unregistered_model_proposal")
                    receipt = getattr(planner, "last_receipt", None)
                    if external and (not isinstance(receipt, dict) or receipt.get("capability") != capability
                                     or receipt.get("provider") != "deepseek"):
                        raise Rejected("model_receipt_required")
                    planned = dict(value, capability=capability)
                    with self.connect() as db:
                        db.execute("UPDATE intake SET status='planned',planned=? WHERE id=?", (canonical(planned), row["id"]))
                        if external:
                            db.execute("UPDATE model_attempts SET status='verified',receipt=? WHERE id=?", (canonical(receipt), row["id"]))
                except Exception:
                    with self.connect() as db:
                        db.execute("UPDATE intake SET status='unknown' WHERE id=?", (row["id"],))
                        if external:
                            db.execute("UPDATE model_attempts SET status='unknown' WHERE id=?", (row["id"],))
                    raise Rejected("planning_unknown_no_replay") from None
            with self.connect() as db:
                pending = db.execute("SELECT id,planned FROM intake WHERE status='planned' ORDER BY rowid LIMIT 16").fetchall()
            for item in pending:
                submit(json.loads(item["planned"]))
                with self.connect() as db:
                    db.execute("UPDATE intake SET status='submitted' WHERE id=?", (item["id"],))
            return len(pending)

    def status(self):
        with self.connect() as db:
            return dict(db.execute("SELECT status,COUNT(*) FROM intake GROUP BY status").fetchall())

    def model_status(self):
        with self.connect() as db:
            return dict(db.execute("SELECT status,COUNT(*) FROM model_attempts GROUP BY status").fetchall())
