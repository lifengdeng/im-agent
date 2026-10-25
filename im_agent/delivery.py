"""Explicit delivery profiles; existing databases retain offline semantics."""
import re

from .contracts import Rejected


def bind_receipts(ledger, kind):
    if kind not in {"offline", "feishu"}:
        raise Rejected("receipt_kind_invalid")
    with ledger.connect() as db:
        table = db.execute("SELECT 1 FROM sqlite_master WHERE name='delivery_profile'").fetchone()
        if table:
            row = db.execute("SELECT kind FROM delivery_profile").fetchone()
            if not row or row[0] != kind:
                raise Rejected("delivery_profile_changed")
        elif kind != "offline":
            # An existing offline ledger requires an explicit migration, never
            # reinterpret its receipts just because a startup flag changed.
            if db.execute("SELECT 1 FROM sqlite_master WHERE name IN ('jobs','outbox')").fetchone():
                raise Rejected("existing_ledger_profile_migration_required")
            db.execute("CREATE TABLE delivery_profile(kind TEXT PRIMARY KEY)")
            db.execute("INSERT INTO delivery_profile VALUES(?)", (kind,))
    return kind


def validate_platform_id(kind, event_id, platform_id, *, sink=False):
    if kind == "offline":
        if not isinstance(platform_id, str) or platform_id != "fake-" + event_id:
            raise Rejected("offline_sink_required" if sink else "offline_receipt_required")
    elif kind != "feishu" or not isinstance(platform_id, str) or not re.fullmatch(r"om_[A-Za-z0-9_-]{1,120}", platform_id):
        raise Rejected("feishu_receipt_required")
