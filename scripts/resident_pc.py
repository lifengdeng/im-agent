#!/usr/bin/env python3
"""Versioned PC deployment of an offline-only resident. No production profile."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
from im_agent.contracts import Rejected, canonical, fingerprint
from im_agent.bundles import inventory
from im_agent.ledger import Host
from im_agent.processes import background_options
from im_agent.resident import ProcessLock, VersionFollower, atomic_json
from im_agent.service import Client, execute_once
from offline_pc import Docker, checked_repository, export, native_plan, run, wheelhouse
from windows_environment import PreparationError, private_acl, reject_links, runtime_path

OWNER = "im-agent-offline-resident-v3"
TASK = "ImAgent-Offline-Resident"


def paths(args):
    try: runtime = runtime_path(str(args.runtime_root))
    except PreparationError as exc:
        runtime = Path(args.runtime_root).absolute().resolve(); reject_links(runtime)
        # The immutable application export deliberately lives under the runtime
        # root. It is not an editable source checkout or a personal Codex home.
        relative = ROOT.relative_to(runtime / "resident/releases") if ROOT.is_relative_to(runtime / "resident/releases") else None
        if str(exc) != "runtime_must_be_outside_source" or relative is None or len(relative.parts) != 2 or relative.parts[1] != "framework" or len(relative.parts[0]) != 24:
            raise
    if json.loads((runtime / "instance.json").read_text(encoding="utf-8")) != {"schema": 1, "profile": "isolated-codex"}:
        raise Rejected("prepared_runtime_identity_required")
    reject_links(runtime)
    state = runtime / "resident"; state.mkdir(exist_ok=True)
    return runtime, state


def prepare(args):
    runtime, state = paths(args)
    core = checked_repository(ROOT, args.commit, generic=True)
    private = checked_repository(args.private_source.resolve(), args.instance_commit)
    if json.loads((private.root / "instance-release.json").read_text(encoding="utf-8"))["framework_commit"] != args.commit:
        raise Rejected("instance_framework_pin_mismatch")
    identifier = fingerprint([args.commit, args.instance_commit])[:24]
    release = state / "releases" / identifier
    if release.exists():
        check_release(state, identifier)
        return {"prepared": identifier, "already_prepared": True}
    staging = state / ("prepare-" + uuid.uuid4().hex); staging.mkdir()
    try:
        framework = staging / "framework"; framework.mkdir()
        instance = staging / "instance"; instance.mkdir()
        export(core, args.commit, framework); export(private, args.instance_commit, instance)
        print(json.dumps({"phase": "snapshot_and_registered_business", "production_enabled": False}), flush=True)
        plan = native_plan(instance, args.native_project, runtime, core.git)
        atomic_json(staging / "declarations.json", plan["declarations"])
        docker = Docker(runtime)
        base = json.loads((framework / "config/offline-runtime.json").read_text())["image"]
        if not base.startswith("python@sha256:") or len(base) != 78: raise Rejected("pinned_base_required")
        try: docker("image", "inspect", base)
        except Rejected: docker("pull", "--platform", "linux/amd64", base, timeout=240)
        wheelhouse(instance, runtime / "downloads/offline-wheels")
        shutil.copytree(framework / "im_agent", instance / "framework/im_agent")
        core_image = "im-agent-offline:" + args.commit[:12]
        image = "im-agent-resident-offline:" + identifier
        for context, tag, file in [(framework, core_image, "offline.Dockerfile"), (instance, image, "resident.Dockerfile")]:
            docker("build", "--network", "none", "--platform", "linux/amd64", "--build-arg", "BASE_IMAGE=" + base,
                   "--label", "im-agent.resident=" + OWNER, "-t", tag, "-f", context / "deploy" / file, context, timeout=240)
        image_id = json.loads(docker("image", "inspect", image))[0]["Id"]
        print(json.dumps({"phase": "offline_regressions", "production_enabled": False}), flush=True)
        regression = {}
        security = ["--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "128", "--memory", "768m", "--tmpfs", "/tmp:rw,nosuid,size=192m"]
        for name, command in [("core", [core_image]),
                ("baseline", ["--entrypoint", "python", image_id, "-B", "scripts/offline_check.py"]),
                ("business", ["--entrypoint", "python", image_id, "-B", "scripts/business_check.py"])]:
            # Captured files are offline fixture output; never credentials.
            checked = subprocess.run([str(a) for a in [*docker.prefix, "run", "--rm", "--pull", "never", "--network", "none", *security, *command]],
                                     env=docker.env, capture_output=True, encoding="utf-8", errors="replace", timeout=240, **background_options())
            output = checked.stdout + checked.stderr
            checks = state / "checks" / identifier; checks.mkdir(parents=True, exist_ok=True)
            (checks / (name + "-regression.txt")).write_text(output, encoding="utf-8")
            if checked.returncode: raise Rejected("offline_regression_failed:" + name + ":report=" + str(checks))
            (staging / (name + "-regression.txt")).write_text(output, encoding="utf-8")
            regression[name] = "passed"
        # Keep release sources, not a second copy of dependency wheels.
        shutil.rmtree(instance / "wheelhouse")
        private_acl(staging)
        manifest = {"schema": 3, "id": identifier, "framework_commit": args.commit, "instance_commit": args.instance_commit,
                    "image_id": image_id, "native_project": str(args.native_project.resolve()), "git": core.git,
                    "python": sys.executable, "production_enabled": False, "regressions": regression,
                    "files": inventory(staging)}
        atomic_json(staging / "release.json", manifest)
        release.parent.mkdir(exist_ok=True); os.replace(staging, release)
        return {"prepared": identifier, "regressions": regression, "production_enabled": False}
    finally:
        if staging.exists(): shutil.rmtree(staging)


def check_release(state, identifier):
    if not isinstance(identifier, str) or len(identifier) != 24 or any(c not in "0123456789abcdef" for c in identifier): raise Rejected("release_id_invalid")
    release = state / "releases" / identifier; reject_links(release)
    manifest = json.loads((release / "release.json").read_text(encoding="utf-8"))
    actual = inventory(release); actual.pop("release.json", None)
    if manifest["id"] != identifier or manifest["schema"] != 3 or manifest["production_enabled"] is not False or actual != manifest["files"]:
        raise Rejected("release_integrity_failed")
    return release, manifest


def selected(state):
    return json.loads((state / "active.json").read_text(encoding="utf-8"))["id"]


def factory(private):
    contract = json.loads((private / "instance-release.json").read_text(encoding="utf-8"))
    name = contract["native_adapter_factory"]; relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name or relative.suffix != ".py":
        raise Rejected("trusted_factory_path_invalid")
    spec = importlib.util.spec_from_file_location("im_agent_resident_private_factory", private.joinpath(*relative.parts))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    if not callable(module.prepare) or not callable(module.observation): raise Rejected("trusted_factory_protocol_missing")
    return module


def activate(args):
    runtime, state = paths(args); release, manifest = check_release(state, args.release)
    stop(args)
    prior = selected(state) if (state / "active.json").exists() else None
    if prior and not (state / "verified" / (prior + ".json")).is_file(): prior = None
    atomic_json(state / "active.json", {"id": args.release, "previous": prior, "schema": 3})
    (state / "stop").unlink(missing_ok=True)
    startup(args, start_now=True)
    return {"activated": args.release, "profile": "offline-only"}


def owned(docker, name):
    found = docker("ps", "-aq", "--filter", "name=^/" + name + "$")
    if not found: return None
    details = json.loads(docker("inspect", name))[0]
    if details["Config"]["Labels"].get("im-agent.resident") != OWNER: raise Rejected("unrelated_container_preserved")
    return details


def containers(runtime, state, release, manifest):
    docker = Docker(runtime)
    network = "im-agent-offline-resident"
    names = {"worker": network + "-worker", "transport": network + "-transport"}
    if not docker("network", "ls", "-q", "--filter", "name=^" + network + "$"):
        docker("network", "create", "--label", "im-agent.resident=" + OWNER, network)
    elif json.loads(docker("network", "inspect", network))[0]["Labels"].get("im-agent.resident") != OWNER:
        raise Rejected("unrelated_network_preserved")
    for directory in ["worker", "transport", "host", "inbox", "secrets"]: (state / directory).mkdir(exist_ok=True)
    secret = state / "secrets/worker.json"
    if not secret.exists(): atomic_json(secret, {"transport": secrets.token_hex(32), "executor": secrets.token_hex(32)})
    tokens = json.loads(secret.read_text(encoding="utf-8"))
    transport_secret = state / "secrets/transport.token"
    if not transport_secret.exists(): transport_secret.write_text(tokens["transport"], encoding="utf-8")
    if transport_secret.read_text(encoding="utf-8") != tokens["transport"]: raise Rejected("service_tokens_inconsistent")
    private_acl(state / "secrets")
    for role, name in names.items():
        prior = owned(docker, name)
        if prior and prior["Config"]["Labels"].get("im-agent.release") != manifest["id"]:
            docker("rm", "-f", name); prior = None
        if not prior:
            mount = lambda source, dest, readonly=False: ["--mount", "type=bind,src=" + str(source) + ",dst=" + dest + (",readonly" if readonly else "")]
            docker("run", "-d", "--pull", "never", "--name", name, "--restart", "unless-stopped",
                "--label", "im-agent.resident=" + OWNER, "--label", "im-agent.release=" + manifest["id"],
                "--network", network, "--network-alias", role,
                *(["-p", "127.0.0.1::8080"] if role == "worker" else []),
                "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "128", "--memory", "768m",
                "--log-opt", "max-size=5m", "--log-opt", "max-file=2", "--tmpfs", "/tmp:rw,nosuid,size=192m",
                *mount(state / role, "/state"), *mount(secret if role == "worker" else transport_secret, "/token", True),
                *mount(release / "declarations.json", "/declarations", True),
                *([] if role == "worker" else mount(state / "inbox", "/inbox", True)),
                manifest["image_id"], role, "--state", "/state", "--token-file", "/token", "--registry", "/declarations",
                *([] if role == "worker" else ["--inbox", "/inbox"]))
        elif not prior["State"]["Running"]: docker("start", name)
    address = docker("port", names["worker"], "8080/tcp").strip()
    if not address.startswith("127.0.0.1:") or "\n" in address: raise Rejected("worker_loopback_required")
    return Client("http://" + address, tokens["executor"]), names


def daemon(args):
    runtime, state = paths(args)
    log = (state / "supervisor.log").open("a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = log
    with ProcessLock(state / "supervisor.lock"):
        identifier = selected(state); release, manifest = check_release(state, identifier)
        # Never execute directly from either editable checkout.
        if ROOT != (release / "framework").resolve(): raise Rejected("immutable_release_runner_required")
        private = release / "instance"
        sys.path.insert(0, str(private))
        provider = factory(private)
        host = Host(state / "host/executions.sqlite3")
        halt = threading.Event(); watcher = None; client = None; last_ensure = 0
        atomic_json(state / "process.json", {"pid": os.getpid(), "release": identifier, "started": time.time()})
        print(canonical({"event": "resident_started", "pid": os.getpid(), "release": identifier, "production_enabled": False}), flush=True)
        try:
            while not (state / "stop").exists():
                try:
                    if time.monotonic() - last_ensure > 15 or client is None:
                        current, names = containers(runtime, state, release, manifest)
                        client = current; last_ensure = time.monotonic()
                        if watcher: watcher.client = client
                    if watcher is None:
                        watcher = VersionFollower(lambda: provider.observation(manifest["native_project"], runtime, manifest["git"]),
                            lambda: provider.prepare(manifest["native_project"], runtime, ROOT, manifest["git"]), host, client, state / "watcher.json")
                        threading.Thread(target=watcher.run, args=(halt, 15), daemon=True).start()
                    if watcher.ready.is_set(): execute_once(client, host)
                    atomic_json(state / "health.json", {"status": "ready" if watcher.ready.is_set() else "preparing_snapshot",
                        "checked_at": time.time(), "pid": os.getpid(), "release": identifier, "production_enabled": False})
                    time.sleep(0.3)
                except Exception:
                    atomic_json(state / "health.json", {"status": "retrying_no_execution_replay", "checked_at": time.time(),
                        "pid": os.getpid(), "release": identifier, "production_enabled": False})
                    time.sleep(2)
        finally:
            halt.set()
            atomic_json(state / "health.json", {"status": "stopped", "checked_at": time.time(), "release": identifier, "production_enabled": False})


def status(args):
    runtime, state = paths(args)
    result = {"profile": "offline-only", "production_enabled": False}
    for name in ["active", "health", "watcher", "process"]:
        path = state / (name + ".json")
        if path.exists(): result[name] = json.loads(path.read_text(encoding="utf-8"))
    try:
        docker = Docker(runtime); names = ["im-agent-offline-resident-worker", "im-agent-offline-resident-transport"]
        details = [owned(docker, n) for n in names]
        result["containers"] = {n: {"running": d["State"]["Running"], "restart_count": d["RestartCount"], "image": d["Image"]} if d else None for n, d in zip(names, details)}
        if all(details):
            address = docker("port", names[0], "8080/tcp").strip()
            token = json.loads((state / "secrets/worker.json").read_text())["executor"]
            client = Client("http://" + address, token)
            result["tasks"] = client.call("/v1/status", {})
            result["conversations"] = client.call("/v1/conversations/status", {})
    except Exception: result["engine_or_api"] = "unavailable"
    path = state / "transport/status.json"
    if path.exists(): result["transport"] = json.loads(path.read_text(encoding="utf-8"))
    return result


def stop(args):
    runtime, state = paths(args)
    (state / "stop").write_text("operator-requested", encoding="utf-8")
    deadline = time.monotonic() + 35
    while True:
        try:
            with ProcessLock(state / "supervisor.lock"): break
        except Rejected:
            if time.monotonic() > deadline: raise Rejected("resident_busy_preserved_no_forced_kill")
            time.sleep(.3)
    docker = Docker(runtime)
    for role in ["transport", "worker"]:
        name = "im-agent-offline-resident-" + role
        if owned(docker, name): docker("stop", "--time", "10", name, timeout=25)
    return {"stopped": True, "ledgers_preserved": True}


def startup(args, *, start_now=False):
    runtime, state = paths(args); release, manifest = check_release(state, selected(state))
    # One narrow, current-user interactive task. Per-user Docker Desktop does not
    # provide a service before Windows logon. No password or SYSTEM token stored.
    script = release / "framework/scripts/register_resident.ps1"
    python = Path(manifest["python"]).with_name("pythonw.exe")
    if not python.is_file(): raise Rejected("windowless_python_required")
    run(["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", script,
         "-RuntimeRoot", runtime, "-Python", python, "-Entry", release / "framework/scripts/resident_pc.py",
         *(["-StartNow"] if start_now else [])], timeout=30)
    return {"task": TASK, "trigger": "current-user-logon", "profile": "offline-only"}


def rollback(args):
    _, state = paths(args); current = json.loads((state / "active.json").read_text())
    if not current.get("previous"): raise Rejected("compatible_previous_release_missing")
    if not (state / "verified" / (current["previous"] + ".json")).is_file(): raise Rejected("previous_release_not_verified")
    args.release = current["previous"]
    result = activate(args); startup(args)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["prepare", "check", "activate", "run", "status", "stop", "startup", "rollback", "verify"])
    p.add_argument("--runtime-root", type=Path, required=True)
    p.add_argument("--private-source", type=Path)
    p.add_argument("--native-project", type=Path)
    p.add_argument("--commit"); p.add_argument("--instance-commit"); p.add_argument("--release")
    args = p.parse_args()
    try:
        if args.command == "check":
            _, state = paths(args); release, manifest = check_release(state, args.release or selected(state)); result = {"checked": manifest["id"]}
        elif args.command == "verify":
            from resident_verify import verify
            result = verify(args)
        else: result = {"prepare": prepare, "activate": activate, "run": daemon, "status": status, "stop": stop, "startup": startup, "rollback": rollback}[args.command](args)
        if result is not None: print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0
    except (Rejected, PreparationError, OSError, subprocess.SubprocessError) as exc:
        if args.command == "run" and str(exc) == "resident_already_running": return 0
        print(str(exc), file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
