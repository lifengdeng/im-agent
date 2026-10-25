#!/usr/bin/env python3
"""Repository preparation only; no application or deployment side effects."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True

from project_checks import CheckError, Repository, check_repository

ROOT = Path(__file__).resolve().parents[1]


def install_hooks(repo: Repository) -> dict:
    previous = repo.value("core.hooksPath")
    if previous not in (None, ".githooks"):
        raise CheckError("existing_hooks_path_preserved")
    for name in ("pre-commit", "pre-push"):
        hook = ROOT / ".githooks" / name
        if not hook.is_file() or hook.is_symlink():
            raise CheckError("versioned_hook_missing_or_invalid")
        if os.name != "nt":
            hook.chmod(hook.stat().st_mode | 0o111)
    repo.run("config", "--local", "im-agent.python", str(Path(sys.executable).resolve()).replace("\\", "/"))
    repo.run("config", "--local", "core.hooksPath", ".githooks")
    return {"installed": True, "scope": "repository", "hooks_path": ".githooks", "python_version": platform.python_version()}


def doctor(repo: Repository) -> dict:
    head = repo.run("rev-parse", "--verify", "HEAD", check=False)
    return {
        "phase": "governance-bootstrap",
        "runtime_implemented": True,
        "candidate_mode": "offline-only",
        "production_ready": False,
        "runtime_ready": False,
        "platform": {"os": platform.system(), "architecture": platform.machine()},
        "python": {"version": platform.python_version(), "executable": sys.executable, "bootstrap_supported": sys.version_info >= (3, 11)},
        "git": {"version": repo.run("--version").stdout.decode("utf-8", "replace").strip(), "executable": repo.git, "commit": head.stdout.decode("ascii", "replace").strip() if head.returncode == 0 else None},
        "hooks_installed": repo.value("core.hooksPath") == ".githooks" and bool(repo.value("im-agent.python")),
        "tools_on_path": {"docker": bool(shutil.which("docker")), "codex": bool(shutil.which("codex"))},
        "docker_engine": "not_probed",
        "services_started": False,
        "business_calls": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="Inspect source without executing it")
    mode = check.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true")
    mode.add_argument("--head", action="store_true")
    sub.add_parser("install-hooks", help="Install repository-local Git hooks")
    doctor_parser = sub.add_parser("doctor", help="Report bootstrap facts without starting services")
    doctor_parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        print("im-agent: Python 3.11 or newer is required", file=sys.stderr)
        return 2
    try:
        repo = Repository(ROOT)
        if args.command == "install-hooks":
            print(json.dumps(install_hooks(repo), ensure_ascii=True))
            return 0
        if args.command == "doctor":
            result = doctor(repo)
            if args.json:
                print(json.dumps(result, ensure_ascii=True, sort_keys=True))
            else:
                print("im-agent bootstrap: " + result["platform"]["os"] + " / Python " + result["python"]["version"])
                print("runtime_ready=false; offline candidate exists; production connectors are not implemented")
                print("hooks_installed=" + str(result["hooks_installed"]).lower())
            return 0
        selected = "staged" if args.staged else "head" if args.head else "worktree"
        result = check_repository(repo, selected)
        status = "passed" if result["passed"] else "failed"
        print(f"im-agent check: {status} ({result['checked_files']} files, mode={selected})")
        for issue in result["issues"][:30]:
            print(json.dumps(issue, ensure_ascii=True), file=sys.stderr)
        if len(result["issues"]) > 30:
            print("additional_issue_count=" + str(len(result["issues"]) - 30), file=sys.stderr)
        return 0 if result["passed"] else 1
    except CheckError as exc:
        print("im-agent: " + str(exc), file=sys.stderr)
        return 2
    except (OSError, subprocess.TimeoutExpired):
        print("im-agent: local_operation_failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
