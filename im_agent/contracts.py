"""Closed offline protocol. Verified identity belongs to transport, never the model."""
import hashlib
import json
import re


class Rejected(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        raise Rejected("identifier_invalid")
    return value


def actor(value):
    if not isinstance(value, dict) or set(value) != {"instance", "agent", "tenant", "user", "chat", "kind"}:
        raise Rejected("actor_invalid")
    for item in value.values():
        identifier(item)
    if value["kind"] not in {"private", "group"}:
        raise Rejected("actor_kind_invalid")
    return value


def envelope(value):
    if not isinstance(value, dict) or set(value) != {"id", "actor", "project", "capability", "input"}:
        raise Rejected("event_invalid")
    identifier(value["id"])
    actor(value["actor"])
    identifier(value["project"]); identifier(value["capability"])
    if not isinstance(value["input"], dict): raise Rejected("capability_input_invalid")
    return value


def event(value, registry=None):
    envelope(value)
    from .adapters import Registry
    (registry or Registry()).descriptor(value)
    return value


def fixture(event_id="synthetic-event"):
    return {"id": event_id, "actor": {"instance": "offline-demo", "agent": "demo", "tenant": "fixture",
            "user": "alice", "chat": "demo-room", "kind": "group"}, "project": "example",
            "capability": "echo", "input": {"text": "offline example"}}
