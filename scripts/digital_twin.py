#!/usr/bin/env python3
"""Prepare and verify an independent digital-twin example. Never manages an existing instance."""
import argparse
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from im_agent.bundles import export_git, inventory, regular
from im_agent.contracts import Rejected, fingerprint
from im_agent.feishu import FeishuAPI, normalize
from im_agent.feishu_fixture import Fixture
from im_agent.processes import background_options
from im_agent.resident import atomic_json
from im_agent.service import Client
from im_agent.twin import TwinTransport, release, run_role
from im_agent.twin_config import load, project_config
from project_checks import CheckError


def compose(root, config):
    uid = os.getuid() if hasattr(os, "getuid") and os.getuid() else 10001
    gid = os.getgid() if hasattr(os, "getgid") and os.getuid() else 10001
    base = json.loads((root / "source/config/offline-runtime.json").read_text(encoding="utf-8"))["image"]
    services = {}
    for role in ["worker", "transport"]:
        arguments = [role, "--runtime", "/runtime", "--state", "/state", "--secrets", "/runtime/services.json"]
        if role == "worker":
            arguments += ["--listen", "0.0.0.0"]
        elif config["profile"] == "offline":
            arguments += ["--worker", "http://worker:8080", "--fixture", "/app/examples/digital-twin/message.example.json", "--once"]
        else:
            arguments += ["--worker", "http://worker:8080", "--connect"]
        # Each role receives only its own token(s), state directory and external key.
        services[role] = {"build": {"context": str(root / "source"), "dockerfile": "deploy/twin.Dockerfile", "args": {"BASE_IMAGE": base}},
            "user": f"{uid}:{gid}", "read_only": True, "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
            "pids_limit": 128, "mem_limit": "512m", "tmpfs": ["/tmp:rw,nosuid,size=64m"], "command": arguments,
            "volumes": [{"type": "bind", "source": str(root / role), "target": "/state"},
                        {"type": "bind", "source": str(root / "manifest.json"), "target": "/runtime/manifest.json", "read_only": True},
                        {"type": "bind", "source": str(root / "secrets" / (role + ".json")), "target": "/runtime/services.json", "read_only": True}],
            "logging": {"driver": "json-file", "options": {"max-size": "1m", "max-file": "2"}}}
    services["worker"]["ports"] = ["127.0.0.1::8080"]
    if config["profile"] == "feishu":
        services["transport"]["environment"] = {config["feishu"]["app_secret_env"]: "${" + config["feishu"]["app_secret_env"] + ":?test app secret required}"}
    if config["planner"]["provider"] == "deepseek":
        key = config["planner"]["api_key_env"]
        services["worker"]["environment"] = {key: "${" + key + ":?model key required}"}
    return {"name": "im-agent-example-" + fingerprint(str(root))[:12], "services": services}


def checked_project(args, config):
    from offline_pc import checked_repository
    repository, commit = args.project_repository, args.project_commit
    if bool(repository) != bool(commit) or bool(repository) != ("project" in config):
        raise Rejected("project_config_and_fixed_git_source_required_together")
    if repository is None:
        return None
    regular(repository.absolute())
    if len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        raise Rejected("fixed_project_commit_required")
    repo = checked_repository(repository, commit)
    if any(entry.startswith(b"160000 ") for entry in repo.run("ls-files", "--stage", "-z").stdout.split(b"\0")):
        raise Rejected("project_submodules_require_separate_release")
    return repo


def export_project(repo, commit, target):
    target.mkdir()
    export_git(repo.git, repo.root, commit, target)
    files = inventory(target)
    if "im_agent_project.py" not in files:
        raise Rejected("project_entrypoint_missing")
    for name in files:
        with (target / name).open("rb") as stream:
            first_line = stream.read(128).split(b"\n", 1)[0].rstrip(b"\r")
            if first_line == b"version https://git-lfs.github.com/spec/v1":
                raise Rejected("project_lfs_requires_materialized_release")
    return {"commit": commit, "files": files}


def prepare(args):
    from offline_pc import checked_repository, export
    config = load(args.instance)
    project_repo = checked_project(args, config)
    root = args.runtime.absolute(); regular(root)
    root = root.resolve()
    if root == ROOT or root.is_relative_to(ROOT) or ROOT.is_relative_to(root):
        raise Rejected("example_runtime_must_be_outside_source")
    if project_repo and (root == project_repo.root or root.is_relative_to(project_repo.root) or project_repo.root.is_relative_to(root)):
        raise Rejected("runtime_must_be_outside_project")
    if root.exists() and any(root.iterdir()):
        raise Rejected("existing_example_runtime_preserved")
    repo = checked_repository(ROOT, args.commit, generic=True)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    source = root / "source"; source.mkdir()
    export(repo, args.commit, source)
    snapshot = fingerprint(inventory(source / "examples/digital-twin/project"))
    manifest = {"schema": 1, "framework_commit": args.commit, "instance": config,
                "snapshot": snapshot, "source_files": inventory(source)}
    if project_repo:
        manifest["project"] = export_project(project_repo, args.project_commit, root / "project")
        manifest["snapshot"] = fingerprint(manifest["project"]["files"])
    atomic_json(root / "manifest.json", manifest)
    for directory in ["worker", "transport", "host", "secrets"]:
        (root / directory).mkdir(mode=0o700)
    tokens = {"transport": secrets.token_hex(32), "executor": secrets.token_hex(32)}
    for role, value in [("worker", tokens), ("transport", {"transport": tokens["transport"]}), ("host", {"executor": tokens["executor"]})]:
        path = root / "secrets" / (role + ".json"); atomic_json(path, value); path.chmod(0o600)
    atomic_json(root / "compose.json", compose(root, config))
    for tree in [source] + ([root / "project"] if project_repo else []):
        for path in tree.rglob("*"):
            if path.is_file():
                path.chmod(0o444)
    if os.name == "nt":
        from windows_environment import private_acl
        private_acl(root)
    return {"prepared": True, "profile": config["profile"], "framework_commit": args.commit, "services_started": False}


def smoke(args):
    source = args.runtime / "source"
    config, snapshot, project = release(args.runtime, source)
    if config["profile"] != "offline":
        raise Rejected("offline_example_required")
    if any((args.runtime / role / "process.lock").exists() for role in ["worker", "host"]):
        raise Rejected("smoke_requires_fresh_runtime")
    raw = json.loads((source / "examples/digital-twin/message.example.json").read_text(encoding="utf-8"))
    if args.question is not None:
        raw["event"]["message"]["content"] = json.dumps({"text": args.question}, ensure_ascii=False)
    fixture = Fixture(raw, config)
    processes = []
    try:
        command = [sys.executable, "-B", "-X", "utf8", str(source / "scripts/digital_twin.py")]
        child_env = {k: v for k, v in os.environ.items() if k in {"SYSTEMROOT", "SystemRoot", "WINDIR", "TEMP", "TMP", "LANG"}}
        child_env["PATH"] = os.defpath
        worker_log = (args.runtime / "worker/process.txt").open("w", encoding="utf-8")
        processes.append(subprocess.Popen(command + ["worker", "--runtime", str(args.runtime), "--state", str(args.runtime / "worker"),
            "--secrets", str(args.runtime / "secrets/worker.json"), "--port", "0"], env=child_env, stdout=worker_log, stderr=worker_log, **background_options()))
        deadline = time.monotonic() + 15
        ready = args.runtime / "worker/ready.json"
        while not ready.exists():
            if processes[0].poll() is not None or time.monotonic() > deadline:
                raise Rejected("example_worker_start_failed")
            time.sleep(.05)
        url = "http://127.0.0.1:" + str(json.loads(ready.read_text(encoding="utf-8"))["port"])
        token = json.loads((args.runtime / "secrets/worker.json").read_text(encoding="utf-8"))
        client = Client(url, token["transport"]); status_client = Client(url, token["executor"])
        api = FeishuAPI(config["feishu"]["app_id"], "synthetic-secret", config["feishu"]["bot_open_id"], fixture_base=fixture.url)
        transport = TwinTransport(args.runtime / "transport", config, snapshot, api)
        value = normalize(raw, config)
        transport.accept(value)
        if transport.accept(value) != {"duplicate": True}:
            raise Rejected("example_duplicate_ingress_failed")
        transport.step(client)
        host_log = (args.runtime / "host/process.txt").open("w", encoding="utf-8")
        processes.append(subprocess.Popen(command + ["host", "--runtime", str(args.runtime), "--state", str(args.runtime / "host"),
            "--secrets", str(args.runtime / "secrets/host.json"), "--worker", url, "--once"], env=child_env, stdout=host_log, stderr=host_log, **background_options()))
        while time.monotonic() < deadline:
            transport.step(client)
            status = status_client.call("/v1/status", {})
            if status["delivered"] == 1:
                break
            time.sleep(.1)
        else:
            raise Rejected("example_roundtrip_timeout")
        if processes[1].wait(timeout=5):
            raise Rejected("example_host_failed")
        restarted = TwinTransport(args.runtime / "transport", config, snapshot, api)
        restarted.accept(value); restarted.step(client)
        if len(fixture.replies) != 1 or api.sends != 1:
            raise Rejected("example_delivery_duplicated")
        final = json.loads(next(iter(fixture.replies.values()))["body"]["content"])["text"]
        report = {"passed": True, "scope": "local-processes-and-loopback-feishu-fixture", "framework_commit": json.loads((args.runtime / "manifest.json").read_text(encoding="utf-8"))["framework_commit"],
            "project": project_config(config)["id"], "snapshot": snapshot,
            "native_subprocess": True, "readback_verified": True, "reply_count": 1,
            "duplicate_and_transport_restart": "passed", "real_feishu_messages": 0, "real_model_calls": 0,
            "containers_verified": False, "result": final}
        atomic_json(args.runtime / "report.json", report)
        return report
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate(); process.wait(timeout=5)
        fixture.close()
        for name in ["worker_log", "host_log"]:
            stream = locals().get(name)
            if stream:
                stream.close()


def status(args):
    config, snapshot, _ = release(args.runtime, args.runtime / "source")
    states = {}
    for role, name, table in [("worker", "worker.sqlite3", "jobs"), ("worker", "planning.sqlite3", "intake"),
                               ("worker", "planning.sqlite3", "model_attempts"),
                               ("transport", "transport.sqlite3", "outbox"), ("host", "executions.sqlite3", "executions")]:
        path = (args.runtime / role / name).resolve()
        if not path.is_file():
            states[role + "/" + table] = None
            continue
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            states[role + "/" + table] = dict(db.execute("SELECT status,COUNT(*) FROM " + table + " GROUP BY status").fetchall()) if exists else None
        finally:
            db.close()
    manifest = json.loads((args.runtime / "manifest.json").read_text(encoding="utf-8"))
    return {"profile": config["profile"], "project": project_config(config)["id"],
            "project_commit": manifest.get("project", {}).get("commit"),
            "snapshot": snapshot, "states": states, "existing_instance_managed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "smoke", "status", "worker", "transport", "host"])
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--instance", type=Path)
    parser.add_argument("--commit")
    parser.add_argument("--project-repository", type=Path)
    parser.add_argument("--project-commit")
    parser.add_argument("--question", help="Synthetic smoke question; never sends a real message")
    parser.add_argument("--state", type=Path)
    parser.add_argument("--secrets", type=Path)
    parser.add_argument("--listen", choices=["127.0.0.1", "0.0.0.0"], default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--worker", default="http://127.0.0.1:8080")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--connect", action="store_true")
    parser.add_argument("--fixture", type=Path)
    args = parser.parse_args()
    if args.command == "prepare" and (args.instance is None or args.commit is None):
        parser.error("prepare requires --instance and --commit")
    if args.command in {"worker", "transport", "host"} and (args.state is None or args.secrets is None):
        parser.error("role requires --state and --secrets")
    try:
        if args.command == "prepare":
            result = prepare(args)
        elif args.command == "smoke":
            result = smoke(args)
        elif args.command == "status":
            result = status(args)
        else:
            result = run_role(args, ROOT)
        if result is not None:
            print(json.dumps(result, ensure_ascii=False))
        return 0
    except (Rejected, CheckError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        # Keep private error detail on disk only; do not print URLs, credentials or messages.
        print("digital-twin: operation_failed; inspect isolated runtime state", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
