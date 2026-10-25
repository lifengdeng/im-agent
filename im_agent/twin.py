"""Independent digital-twin example roles; existing resident entry points are unchanged."""
import json
import os
from pathlib import Path
import threading
import time

from .bundles import inventory, regular
from .contracts import Rejected, fingerprint
from .feishu import FeishuAPI, normalize, receive
from .intake import Inbox
from .ledger import Host, Transport, Worker
from .resident import ProcessLock, atomic_json
from .service import Client, execute_once, server
from .twin_config import capability_names, registry, validate
from .twin_planner import Planner
from .twin_project import Project


def release(runtime, source, *, verify_project=True):
    runtime, source = Path(runtime).resolve(), Path(source).resolve()
    regular(runtime); regular(source)
    manifest = json.loads((runtime / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != 1 or inventory(source) != manifest["source_files"]:
        raise Rejected("example_release_integrity_failed")
    config = validate(manifest["instance"])
    project = source / "examples/digital-twin/project"
    if "project" in config:
        project = runtime / "project"
        metadata = manifest.get("project", {})
        if (set(metadata) != {"commit", "files"} or not isinstance(metadata["commit"], str)
                or len(metadata["commit"]) != 40 or any(c not in "0123456789abcdef" for c in metadata["commit"])
                or not isinstance(metadata["files"], dict) or "im_agent_project.py" not in metadata["files"]
                or fingerprint(metadata["files"]) != manifest["snapshot"]):
            raise Rejected("project_release_manifest_invalid")
    if verify_project and fingerprint(inventory(project)) != manifest["snapshot"]:
        raise Rejected("example_snapshot_integrity_failed")
    return config, manifest["snapshot"], project


class TwinWorker(Worker):
    def __init__(self, state, config, snapshot, planner=None):
        self.planner = planner or Planner(config)
        self.config = config
        self.intake = Inbox(state / "planning.sqlite3", registry(config, snapshot, intake=True),
                            recover_planning=True, capabilities=capability_names(config))
        super().__init__(state / "worker.sqlite3", registry(config, snapshot), receipt_kind="feishu")

    def step(self):
        return self.intake.plan(self.planner, self.ingress)

    def status(self):
        value = super().status()
        attempts = self.intake.model_status()
        return dict(value, mode="digital-twin-" + self.config["profile"], production_enabled=False,
                    model_calls=sum(attempts.values()), model_attempts=attempts, planning=self.intake.status())


class TwinTransport:
    def __init__(self, state, config, snapshot, api):
        reg = registry(config, snapshot, intake=True)
        self.ledger = Transport(state / "transport.sqlite3", reg, receipt_kind="feishu")
        self.inbox = Inbox(state / "intake.sqlite3", reg)
        self.api, self.config = api, config

    def accept(self, value):
        self.ledger.receive(value)
        return self.inbox.accept(value)

    def step(self, client):
        self.inbox.forward(lambda value: client.call("/v1/twin/ingress", value))
        for intent in client.call("/v1/intents", {}):
            try:
                receipt = self.ledger.deliver(intent, self.api.reply)
            except Rejected as exc:
                if str(exc) == "unknown_delivery_requires_reconciliation":
                    continue
                raise
            client.call("/v1/receipt", receipt)
        return self.status()

    def status(self):
        with self.ledger.connect() as db:
            delivery = dict(db.execute("SELECT status,COUNT(*) FROM outbox GROUP BY status").fetchall())
        return {"intake": self.inbox.status(), "delivery": delivery}


def host(state, config, snapshot, project):
    reg = registry(config, snapshot)
    adapter = Project(project, snapshot, config["persona"], config.get("project"))
    for descriptor in list(reg.versions.values()):
        reg.add(descriptor, adapter.execute, contextual=True)
    return Host(state / "executions.sqlite3", reg)


def run_role(args, source):
    config, snapshot, project = release(args.runtime, source, verify_project=args.command == "host")
    if config["profile"] == "offline" and args.command in {"worker", "transport"}:
        from .offline_network import install
        install(args.command)
    state = args.state.resolve(); regular(state); state.mkdir(parents=True, exist_ok=True)
    if state == source or state.is_relative_to(source):
        raise Rejected("example_state_must_be_outside_source")
    secrets = json.loads(args.secrets.read_text(encoding="utf-8"))
    with ProcessLock(state / "process.lock"):
        if args.command == "worker":
            worker = TwinWorker(state, config, snapshot)
            http = server((args.listen, args.port), worker, secrets, {"/v1/twin/ingress": ("transport", worker.intake.accept)})
            stopped = threading.Event()

            def planning():
                while not stopped.wait(.1):
                    try:
                        worker.step()
                    except Exception:
                        atomic_json(state / "health.json", {"status": "planning_requires_reconciliation", "planning": worker.intake.status()})

            thread = threading.Thread(target=planning, daemon=True); thread.start()
            atomic_json(state / "ready.json", {"port": http.server_port, "profile": config["profile"]})
            try:
                http.serve_forever()
            finally:
                stopped.set(); http.server_close(); thread.join(timeout=31)
        elif args.command == "host":
            client = Client(args.worker, secrets["executor"])
            executor = host(state, config, snapshot, project)
            deadline = time.monotonic() + 30
            while True:
                try:
                    client.call("/v1/status", {})
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise Rejected("example_worker_unavailable") from None
                    time.sleep(.2)
            while True:
                if execute_once(client, executor) and args.once:
                    return
                if args.once and time.monotonic() > deadline:
                    raise Rejected("example_host_timeout")
                time.sleep(.1)
        else:
            if config["profile"] == "offline":
                if args.connect or not args.fixture or args.fixture.resolve() != (source / "examples/digital-twin/message.example.json").resolve():
                    raise Rejected("explicit_synthetic_fixture_required")
                from .feishu_fixture import Fixture
                raw = json.loads(args.fixture.read_text(encoding="utf-8"))
                fixture = Fixture(raw, config)
                try:
                    api = FeishuAPI(config["feishu"]["app_id"], "synthetic-secret", config["feishu"]["bot_open_id"], fixture_base=fixture.url)
                    transport = TwinTransport(state, config, snapshot, api)
                    value = normalize(raw, config)
                    transport.accept(value); transport.accept(value)
                    client = Client(args.worker, secrets["transport"])
                    deadline = time.monotonic() + 60
                    while time.monotonic() < deadline:
                        try:
                            status = transport.step(client)
                        except OSError:
                            time.sleep(.2); continue
                        if status["delivery"].get("verified") == 1:
                            # ACK again after reconstructing the durable transport.
                            restarted = TwinTransport(state, config, snapshot, api)
                            restarted.accept(value); restarted.step(client)
                            if api.sends != 1 or len(fixture.replies) != 1:
                                raise Rejected("example_fixture_delivery_duplicated")
                            atomic_json(state / "report.json", {"passed": True, "readback_verified": True,
                                "reply_count": 1, "real_feishu_messages": 0, "real_model_calls": 0})
                            return
                        time.sleep(.2)
                    raise Rejected("example_fixture_roundtrip_timeout")
                finally:
                    fixture.close()
            if config["profile"] != "feishu" or not args.connect:
                raise Rejected("explicit_test_app_connection_required")
            secret = os.environ.get(config["feishu"]["app_secret_env"], "")
            if len(secret) < 12:
                raise Rejected("feishu_secret_missing")
            api = FeishuAPI(config["feishu"]["app_id"], secret, config["feishu"]["bot_open_id"])
            transport = TwinTransport(state, config, snapshot, api)
            client = Client(args.worker, secrets["transport"])

            def forwarding():
                while True:
                    try:
                        status = transport.step(client)
                        atomic_json(state / "health.json", dict(status,
                            status="delivery_requires_reconciliation" if status["delivery"].get("unknown") else "running"))
                    except Exception:
                        atomic_json(state / "health.json", {"status": "delivery_or_forwarding_requires_reconciliation"})
                    time.sleep(.2)

            threading.Thread(target=forwarding, daemon=True).start()
            receive(config, transport.accept, secret)
