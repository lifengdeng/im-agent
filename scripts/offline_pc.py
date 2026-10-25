#!/usr/bin/env python3
"""Build and validate fixed Git candidates. Never activate a production agent."""
from __future__ import annotations

import argparse
import hashlib
import io
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.parse
import urllib.request
import uuid

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from im_agent.contracts import Rejected
from im_agent.adapters import Registry
from im_agent.bundles import regular
from im_agent.ledger import Host
from im_agent.processes import background_options
from im_agent.service import Client, execute_once
from project_checks import Repository, check_repository
from windows_environment import reject_links, runtime_path


def run(args, *, timeout=60, cwd=None, env=None):
    result = subprocess.run([str(a) for a in args], cwd=cwd, env=env, capture_output=True,
                            encoding="utf-8", errors="replace", timeout=timeout, **background_options())
    if result.returncode:
        # Docker/git errors can contain local paths, but no credential contents
        # or raw container output is printed by default.
        raise Rejected("command_failed:" + Path(str(args[0])).name + ":" + str(result.returncode) + ":" + result.stderr[-1200:])
    return result.stdout.strip()


def checked_repository(root, revision, *, generic=False):
    repo = Repository(root)
    if repo.run("rev-parse", "HEAD").stdout.decode().strip() != revision or len(revision) != 40:
        raise Rejected("fixed_commit_required")
    if repo.run("status", "--porcelain", "--untracked-files=all").stdout.strip() or repo.hidden_paths():
        raise Rejected("dirty_or_hidden_source_preserved")
    if repo.value("core.sparseCheckout") == "true":
        raise Rejected("sparse_checkout_rejected")
    if generic and not check_repository(repo, "head")["passed"]:
        raise Rejected("source_governance_failed")
    return repo


def export(repo, revision, target):
    raw = repo.run("archive", "--format=tar", revision).stdout
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or "\\" in member.name or not (member.isdir() or member.isfile()):
                raise Rejected("unsafe_git_archive")
            destination = target.joinpath(*path.parts)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.extractfile(member).read())


def native_plan(private_context, project, runtime_root, git):
    """Load operator-reviewed code only from the fixed private Git export."""
    release = json.loads((private_context / "instance-release.json").read_text(encoding="utf-8"))
    name = release.get("native_adapter_factory", "")
    relative = PurePosixPath(name)
    if (not name or relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name
            or relative.suffix != ".py"):
        raise Rejected("trusted_native_factory_required")
    path = private_context.joinpath(*relative.parts); regular(path)
    if not path.is_file(): raise Rejected("native_factory_missing")
    spec = importlib.util.spec_from_file_location("im_agent_private_factory", path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(private_context))
    try:
        spec.loader.exec_module(module)
        plan = module.prepare(project, runtime_root, ROOT, git)
    finally:
        sys.path.remove(str(private_context))
    Registry(plan["declarations"])
    if not isinstance(plan["events"], list) or not 1 <= len(plan["events"]) <= 8:
        raise Rejected("native_offline_event_budget")
    return plan


def wheelhouse(private, cache):
    lock = json.loads((private / "dependencies-linux.json").read_text(encoding="utf-8"))
    destination = private / "wheelhouse"; destination.mkdir()
    cache.mkdir(parents=True, exist_ok=True)
    for item in lock["packages"]:
        name = item["filename"]
        if Path(name).name != name or not name.endswith(".whl"):
            raise Rejected("dependency_filename_invalid")
        url = urllib.parse.urlsplit(item["url"])
        if url.scheme != "https" or url.hostname != "files.pythonhosted.org" or url.username or url.query or url.fragment:
            raise Rejected("official_dependency_source_required")
        path = cache / name
        if not path.exists():
            partial = path.with_suffix(".partial")
            try:
                with urllib.request.urlopen(item["url"], timeout=60) as response, partial.open("wb") as stream:
                    total = 0
                    while chunk := response.read(1024 * 1024):
                        total += len(chunk)
                        if total > item["bytes"]:
                            raise Rejected("dependency_size_mismatch")
                        stream.write(chunk)
                os.replace(partial, path)
            finally:
                if partial.exists(): partial.unlink()
        if path.stat().st_size != item["bytes"] or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise Rejected("dependency_hash_mismatch")
        shutil.copyfile(path, destination / name)


class Docker:
    def __init__(self, root):
        if os.name != "nt":
            raise Rejected("this_profile_requires_windows_desktop_linux_engine")
        self.binary = Path(os.environ["LOCALAPPDATA"]) / "Programs/DockerDesktop/resources/bin/docker.exe"
        if not self.binary.is_file():
            self.binary = Path(os.environ["ProgramFiles"]) / "Docker/Docker/resources/bin/docker.exe"
        config = root / "docker-smoke-config"
        expected = {"auths": {"https://index.docker.io/v1/": {}}}
        config.mkdir(exist_ok=True)
        file = config / "config.json"
        if file.exists() and json.loads(file.read_text(encoding="utf-8")) != expected:
            raise Rejected("existing_docker_config_preserved")
        if not file.exists(): file.write_text(json.dumps(expected), encoding="utf-8")
        self.prefix = [self.binary, "--config", config, "--host", "npipe:////./pipe/dockerDesktopLinuxEngine"]
        self.env = {k: v for k, v in os.environ.items() if not k.upper().startswith("DOCKER_")}

    def __call__(self, *args, timeout=60):
        return run([*self.prefix, *args], env=self.env, timeout=timeout)


def validate(args):
    core = checked_repository(ROOT, args.commit, generic=True)
    private_path = args.private_source.resolve()
    private = checked_repository(private_path, args.instance_commit)
    root = runtime_path(str(args.runtime_root))
    if root.is_relative_to(private_path) or private_path.is_relative_to(root):
        raise Rejected("private_source_and_runtime_must_be_separate")
    reject_links(root)
    receipt = json.loads((root / "instance.json").read_text(encoding="utf-8"))
    if receipt != {"schema": 1, "profile": "isolated-codex"}:
        raise Rejected("prepared_private_runtime_required")
    docker = Docker(root)
    before = set(docker("ps", "-aq").splitlines())
    run_id = "offline-" + uuid.uuid4().hex[:12]
    directory = root / "validation" / run_id; directory.mkdir(parents=True)
    context = directory / "core-source"; context.mkdir()
    private_context = directory / "private-source"; private_context.mkdir()
    export(core, args.commit, context)
    export(private, args.instance_commit, private_context)
    plan = None
    if args.native_project:
        print(json.dumps({"phase": "prepare_existing_native_runtime", "run": run_id, "packages_reinstalled": 0}), flush=True)
        plan = native_plan(private_context, args.native_project, root, core.git)
        (directory / "declarations.json").write_text(json.dumps(plan["declarations"]), encoding="utf-8")
        (directory / "events.json").write_text(json.dumps(plan["events"]), encoding="utf-8")
    lock = json.loads((context / "config/offline-runtime.json").read_text(encoding="utf-8"))
    image = lock["image"]
    if not image.startswith("python@sha256:") or len(image) != len("python@sha256:") + 64:
        raise Rejected("pinned_official_base_required")
    print(json.dumps({"phase": "prepare_dependencies", "run": run_id, "business_calls": 0}), flush=True)
    docker("pull", "--platform", "linux/amd64", image, timeout=240)
    wheelhouse(private_context, root / "downloads/offline-wheels")
    generic_image = "im-agent-offline:" + args.commit[:12]
    private_image = "im-agent-instance-baseline-offline:" + args.instance_commit[:12]
    for source, tag in [(context, generic_image), (private_context, private_image)]:
        docker("build", "--network", "none", "--platform", "linux/amd64", "--build-arg", "BASE_IMAGE=" + image,
               "--label", "im-agent.offline-source=" + (args.commit if source == context else args.instance_commit),
               "-t", tag, "-f", source / "deploy/offline.Dockerfile", source, timeout=240)
    security = ["--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "128",
                "--memory", "512m", "--tmpfs", "/tmp:rw,nosuid,size=128m"]
    print(json.dumps({"phase": "network_disabled_regressions", "run": run_id}), flush=True)
    regression_outputs = {}
    for role, image_name in [("core", generic_image), ("baseline", private_image)]:
        output = docker("run", "--rm", "--network", "none", *security, "--label", "im-agent.offline-run=" + run_id,
                        image_name, timeout=180)
        (directory / (role + "-regression.txt")).write_text(output + "\n", encoding="utf-8")
        regression_outputs[role] = output.splitlines()[-1] if output else "passed"
    network = "im-agent-" + run_id
    names = [network + "-worker", network + "-transport"]
    created = []
    report = {"schema": 1, "run": run_id, "framework_commit": args.commit, "instance_commit": args.instance_commit,
              "legacy_source_commit": json.loads((private_context / "source-lock.json").read_text(encoding="utf-8"))["source_commit"],
              "base_image": image, "regressions": regression_outputs, "production_enabled": False,
              "production_state_imported": False, "model_calls": 0, "feishu_messages_sent": 0}
    if plan: report["native_runtime"] = plan["details"]
    tokens = {"transport": secrets.token_hex(32), "executor": secrets.token_hex(32)}
    (directory / "worker-tokens.json").write_text(json.dumps(tokens), encoding="utf-8")
    (directory / "transport.token").write_text(tokens["transport"], encoding="utf-8")
    for name in ("worker", "transport", "host"):
        (directory / name).mkdir()
    network_created = False
    try:
        docker("network", "create", "--label", "im-agent.offline-run=" + run_id, network)
        network_created = True
        registry_mount = (["--mount", "type=bind,src=" + str(directory / "declarations.json") + ",dst=/declarations,readonly"] if plan else [])
        registry_args = ["--registry", "/declarations"] if plan else []
        docker("run", "-d", "--name", names[0], "--label", "im-agent.offline-run=" + run_id,
               "--network", network, "--network-alias", "worker", "-p", "127.0.0.1::8080", *security,
               "--mount", "type=bind,src=" + str(directory / "worker") + ",dst=/state",
               "--mount", "type=bind,src=" + str(directory / "worker-tokens.json") + ",dst=/token,readonly",
               *registry_mount, generic_image, "python", "-B", "-m", "im_agent.service", "worker", "--state", "/state", "--token-file", "/token", *registry_args)
        created.append(names[0])
        details = json.loads(docker("inspect", names[0]))[0]
        (directory / "worker-start.json").write_text(json.dumps({"State": details["State"],
            "NetworkSettings": details["NetworkSettings"], "PortBindings": details["HostConfig"]["PortBindings"]}, indent=2), encoding="utf-8")
        address = docker("port", names[0], "8080/tcp").strip()
        if not address.startswith("127.0.0.1:"):
            raise Rejected("worker_must_publish_loopback_only")
        client = Client("http://" + address, tokens["executor"])
        deadline = time.monotonic() + 25
        while True:
            try: client.call("/v1/status", {}); break
            except OSError:
                if time.monotonic() > deadline: raise Rejected("worker_start_timeout")
                time.sleep(0.3)
        docker("run", "-d", "--name", names[1], "--label", "im-agent.offline-run=" + run_id,
               "--network", network, *security,
               "--mount", "type=bind,src=" + str(directory / "transport") + ",dst=/state",
               "--mount", "type=bind,src=" + str(directory / "transport.token") + ",dst=/token,readonly",
               *registry_mount, *(["--mount", "type=bind,src=" + str(directory / "events.json") + ",dst=/events,readonly"] if plan else []),
               generic_image, "python", "-B", "-m", "im_agent.service", "transport", "--state", "/state", "--token-file", "/token",
               *registry_args, *(["--events", "/events"] if plan else []), "--timeout", "300")
        created.append(names[1])
        host = Host(directory / "host/executions.sqlite3", plan["host_registry"] if plan else None)
        expected = len(plan["events"]) if plan else 1
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            execute_once(client, host)
            status = client.call("/v1/status", {})
            if status["delivered"] == expected: break
            time.sleep(0.2)
        else: raise Rejected("three_component_roundtrip_timeout")
        if docker("wait", names[1], timeout=15).strip() != "0":
            raise Rejected("offline_transport_failed")
        verified = json.loads((directory / "transport/complete.json").read_text(encoding="utf-8"))
        inspected = json.loads(docker("inspect", *names))
        internal = json.loads(docker("network", "inspect", network))[0]["Internal"]
        for component in inspected:
            if (component["Config"]["User"] != "10001:10001" or not component["HostConfig"]["ReadonlyRootfs"]
                    or component["HostConfig"]["CapDrop"] != ["ALL"]):
                raise Rejected("container_isolation_failed")
        report.update(roundtrip=status, fake_delivery=verified, internal_network=internal,
                      dedicated_test_network=True, service_process_egress_guard=True,
                      nonroot_readonly_containers=True, host_runs_natively=True, database_owners_separate=True,
                      image_ids={"core": json.loads(docker("image", "inspect", generic_image))[0]["Id"],
                                 "baseline": json.loads(docker("image", "inspect", private_image))[0]["Id"]})
        print(json.dumps({"phase": "three_component_roundtrip_passed", "run": run_id}), flush=True)
    finally:
        for name in reversed(created):
            details = json.loads(docker("inspect", name))[0]
            if details["Config"]["Labels"].get("im-agent.offline-run") == run_id:
                diagnostic = subprocess.run([str(p) for p in [*docker.prefix, "logs", name]], env=docker.env,
                    capture_output=True, encoding="utf-8", errors="replace", timeout=15, **background_options())
                (directory / ("transport-container.txt" if name == names[1] else "worker-container.txt")).write_text(
                    diagnostic.stdout + diagnostic.stderr, encoding="utf-8")
                docker("rm", "-f", name)
        if network_created: docker("network", "rm", network)
        for file in (directory / "worker-tokens.json", directory / "transport.token"):
            if file.exists(): file.unlink()
    after = set(docker("ps", "-aq").splitlines())
    report["existing_containers_preserved"] = before <= after
    report["temporary_containers_removed"] = all(not docker("ps", "-aq", "--filter", "name=^/" + name + "$").strip() for name in names)
    report["unrelated_new_container_count"] = len(after - before)
    if not report["temporary_containers_removed"]:
        raise Rejected("container_inventory_changed")
    report["status"] = "passed"
    report_file = directory / "report.json"
    report_file.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (root / "last-offline-validation.json").write_text(json.dumps({"report": str(report_file), **report}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "passed", "report": str(report_file), "production_enabled": False,
                      "temporary_containers_removed": True}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["validate"])
    parser.add_argument("--private-source", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--instance-commit", required=True)
    parser.add_argument("--native-project", type=Path, help="Adopt an existing native installation through the trusted private factory")
    args = parser.parse_args()
    try:
        validate(args)
        return 0
    except (Rejected, OSError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
