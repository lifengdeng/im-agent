"""Process ownership and asynchronous trusted version publication."""
import json
import os
from pathlib import Path
import threading
import time

from .adapters import Registry
from .contracts import Rejected, canonical, fingerprint


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + ".new")
    temp.write_text(canonical(value), encoding="utf-8")
    os.replace(temp, path)


class ProcessLock:
    """OS lifetime lock, released even after a process crash; PID is informational."""
    def __init__(self, path):
        self.path = Path(path)

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.file.seek(0, 2)
        if not self.file.tell(): self.file.write(b"0"); self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise Rejected("resident_already_running") from None
        return self

    def __exit__(self, *args):
        if os.name == "nt":
            import msvcrt
            self.file.seek(0); msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        self.file.close()


class VersionFollower:
    """Prepare off the intake thread, keep historical handlers before publishing.

    A failed or unknown publication is retried idempotently. It never retries an
    execution. The trusted factory owns release criteria and frozen dependencies.
    """
    def __init__(self, inspect, prepare, host, client, status_file):
        self.inspect, self.prepare, self.host, self.client = inspect, prepare, host, client
        self.status_file = Path(status_file)
        self.observed, self.pending, self.ready = None, None, threading.Event()
        self.details = None

    def step(self):
        try:
            observation = self.inspect()
            if self.pending is None and (self.observed != observation or not self.ready.is_set()):
                plan = self.prepare()
                old = self.host.registry
                incoming = plan["host_registry"]
                # The factory must retain executable handlers for every old contract.
                if set(old.versions) - set(incoming.versions): raise Rejected("historical_host_contract_missing")
                if set(incoming.versions) - set(incoming.handlers): raise Rejected("historical_host_handler_missing")
                self.host.registry = incoming
                self.pending = (observation, plan)
            if self.pending:
                observation, plan = self.pending
                current = self.client.call("/v1/registry", {})
                declarations = Registry(plan["declarations"]).declarations()
                if fingerprint(current["declarations"]) != fingerprint(declarations):
                    self.client.call("/v1/registry/publish", {"generation": current["generation"] + 1, "declarations": declarations})
                self.observed = None if plan.get("retry_candidate") else observation
                self.details, self.pending = plan["details"], None
                self.ready.set()
            atomic_json(self.status_file, {"status": "ready", "checked_at": time.time(), "details": self.details,
                                          "production_enabled": False})
            return True
        except Exception:
            atomic_json(self.status_file, {"status": "retrying_keep_previous", "checked_at": time.time(),
                                          "has_previous": self.ready.is_set(), "production_enabled": False})
            return False

    def run(self, stop, interval=15):
        while not stop.is_set():
            self.step()
            stop.wait(interval)
