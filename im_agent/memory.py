"""Explicit, scoped memory. Tombstones are replayed after a backup restore."""
import json
import time

from .contracts import Rejected, actor, canonical, fingerprint, identifier
from .ledger import Database


def scope_key(who, shared=False):
    actor(who)
    if shared and who["kind"] != "group":
        raise Rejected("shared_memory_requires_group")
    return canonical({k: who[k] for k in ("instance", "agent", "tenant", "chat")} | {"principal": "group" if shared else who["user"], "kind": who["kind"]})


class Memory(Database):
    def __init__(self, path):
        super().__init__(path, "worker-memory-v1")
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS memories(scope TEXT,key TEXT,author TEXT,value TEXT,revision INTEGER,deleted INTEGER,
                    PRIMARY KEY(scope,key));
                CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY,digest TEXT NOT NULL,receipt TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS deletions(scope TEXT,key TEXT,revision INTEGER,PRIMARY KEY(scope,key));
            """)

    def change(self, who, key, value, request_id, *, shared=False, explicit=False, forget=False):
        scope = scope_key(who, shared)
        identifier(key); identifier(request_id)
        if not explicit or not isinstance(value, str) or len(value) > 2000:
            raise Rejected("explicit_memory_request_required")
        request = fingerprint([scope, key, value, who["user"], forget])
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM receipts WHERE id=?", (request_id,)).fetchone()
            if prior:
                if prior["digest"] != request:
                    raise Rejected("memory_request_collision")
                return json.loads(prior["receipt"])
            row = db.execute("SELECT * FROM memories WHERE scope=? AND key=?", (scope, key)).fetchone()
            if row and row["author"] != who["user"]:
                raise Rejected("memory_owner_mismatch")
            revision = row["revision"] + 1 if row else 1
            db.execute("INSERT OR REPLACE INTO memories VALUES(?,?,?,?,?,?)",
                       (scope, key, who["user"], None if forget else value, revision, int(forget)))
            if forget:
                db.execute("INSERT OR REPLACE INTO deletions VALUES(?,?,?)", (scope, key, revision))
                # Receipts contain hashes and revision/status only. Keeping
                # them prevents replay of an old remember after forgetting.
            receipt = {"key": key, "revision": revision, "status": "forgotten" if forget else "remembered"}
            db.execute("INSERT INTO receipts VALUES(?,?,?)", (request_id, request, canonical(receipt)))
        return receipt

    def recall(self, who, *, shared=False):
        scope = scope_key(who, shared)
        with self.connect() as db:
            rows = db.execute("SELECT key,value,revision FROM memories WHERE scope=? AND deleted=0 ORDER BY key LIMIT 8", (scope,)).fetchall()
        return [dict(r) for r in rows]

    def tombstones(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM deletions")]

    def apply_tombstones(self, values):
        with self.connect() as db:
            for row in values:
                db.execute("UPDATE memories SET deleted=1,value=NULL,revision=? WHERE scope=? AND key=? AND revision<=?",
                           (row["revision"], row["scope"], row["key"], row["revision"]))
                db.execute("INSERT OR REPLACE INTO deletions VALUES(?,?,?)", (row["scope"], row["key"], row["revision"]))
