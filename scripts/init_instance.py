#!/usr/bin/env python3
"""Create an independent instance, configure it locally, and verify offline first."""
import argparse
import copy
from contextlib import nullcontext
import getpass
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import warnings

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from im_agent.bundles import regular
from im_agent.contracts import Rejected
from im_agent.feishu import FeishuAPI, receive_events
from im_agent.feishu_fixture import Fixture
from im_agent.onboarding import Instance, Pairing, MODEL_ENV, SECRET_ENV, independent_path, read_json, secret_value, write_private
from im_agent.processes import background_options
from im_agent.resident import ProcessLock
from im_agent.twin_config import load, project_config, validate
from digital_twin import checked_project, export_project, prepare, smoke
from project_checks import CheckError, Repository


def terminal():
    if not sys.stdin.isatty():
        raise Rejected("interactive_terminal_required_use_defaults_or_environment_references")


def ask_secret(prompt):
    terminal()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass(prompt)
    except getpass.GetPassWarning:
        raise Rejected("secure_secret_input_unavailable") from None


def selection(instance, repository, definition):
    repository = Path(repository).expanduser().absolute(); regular(repository)
    repository = repository.resolve()
    if repository.is_relative_to(instance.root) or instance.root.is_relative_to(repository):
        raise Rejected("project_and_instance_must_be_separate")
    if repository.is_relative_to(ROOT) or ROOT.is_relative_to(repository):
        raise Rejected("selected_project_must_be_independent")
    commit = Repository(repository).run("rev-parse", "HEAD").stdout.decode("ascii").strip()
    config = copy.deepcopy(load(instance.root / "instance.json"))
    config["project"] = read_json(Path(definition).expanduser().absolute()); validate(config)
    args = SimpleNamespace(project_repository=repository, project_commit=commit)
    repo = checked_project(args, config)
    # Inspect the fixed export, including entrypoint, LFS, links and private-state rules.
    # This never imports or executes the selected project's code.
    with tempfile.TemporaryDirectory(prefix="im-agent-project-check-") as temporary:
        export_project(repo, commit, Path(temporary).resolve() / "project")
    return ({"repository": str(repository), "commit": commit}, config["project"])


def configure(instance, args):
    instance.mutable()
    app_id, app_secret, provider, model, model_key, project = args.app_id, None, args.provider, args.model, None, None
    if args.non_interactive:
        if bool(app_id) != bool(args.app_secret_env):
            raise Rejected("app_id_and_secret_environment_reference_required_together")
        if args.app_secret_env:
            app_secret = secret_value(os.environ.get(args.app_secret_env))
        if provider == "deepseek":
            model_key = secret_value(os.environ.get(args.model_key_env))
        elif args.model_key_env or model:
            raise Rejected("model_options_require_deepseek")
        if bool(args.project_repository) != bool(args.project_definition):
            raise Rejected("project_and_definition_required_together")
        if args.project_repository:
            project = selection(instance, args.project_repository, args.project_definition)
        elif args.builtin_project:
            project = (None, None)
    else:
        terminal()
        print("凭据只保存在实例 .private/，输入不回显；留空可稍后配置。")
        app_id = input("独立飞书应用 App ID（留空保留）：").strip() or None
        if app_id:
            app_secret = ask_secret("App Secret：")
        current = instance.settings()["planner"]["provider"]
        provider = input(f"模型路由 rules / deepseek（留空保留 {current}）：").strip() or None
        if provider == "deepseek":
            model = input("DeepSeek 模型名称（按服务商当前可用名称填写）：").strip()
            model_key = ask_secret("模型 API Key：")
        value = input("本地工程 Git 目录（留空保留；example 使用合成工程）：").strip()
        if value == "example":
            project = (None, None)
        elif value:
            definition = input("能力声明 JSON 路径（格式见 project-template/capabilities.json）：").strip()
            project = selection(instance, value, definition)
            print(json.dumps({"project": project[1], "fixed_commit": project[0]["commit"]}, ensure_ascii=False, indent=2))
            if input("允许该工程在宿主执行上述注册能力？输入 YES 确认：").strip() != "YES":
                raise Rejected("local_project_selection_not_approved")
    instance.configure(app_id=app_id, app_secret=app_secret, provider=provider, model=model, model_key=model_key, project=project)
    return instance.status()


def runtime_path(instance, value):
    value = independent_path(value, ROOT)
    if value.is_relative_to(instance.root) or instance.root.is_relative_to(value):
        raise Rejected("runtime_and_instance_must_be_separate")
    if value.exists():
        raise Rejected("existing_runtime_preserved")
    return value


def prepare_args(instance, runtime, commit, config, *, selected=False):
    project = instance.settings()["project_source"] if selected else None
    return SimpleNamespace(instance=config, runtime=runtime, commit=commit,
        project_repository=Path(project["repository"]) if project else None,
        project_commit=project["commit"] if project else None)


def demo(instance, args):
    runtime = runtime_path(instance, args.runtime)
    config = load(instance.root / "instance.json")
    if not args.selected_project:
        config.pop("project", None)
    path = instance.private / "demo.instance.json"
    write_private(path, config)
    prepare(prepare_args(instance, runtime, args.commit, path, selected=args.selected_project))
    # The pairing codec and original-message readback run against loopback HTTP.
    pairing, code = instance.begin_pairing("example-bot", purpose="offline")
    raw = read_json(ROOT / "examples/digital-twin/message.example.json")
    raw["event"]["message"].update(content=json.dumps({"text": "/pair " + code}), create_time=str(int(time.time() * 1000)))
    fixture = Fixture(raw, config)
    try:
        api = FeishuAPI("example-app", "synthetic-secret", "example-bot", fixture_base=fixture.url)
        if api.bot_info() != "example-bot" or not pairing.accept(raw, api):
            raise Rejected("offline_pairing_failed")
        paired = pairing.approve(True)
    finally:
        fixture.close()
    report = smoke(SimpleNamespace(runtime=runtime, question=args.question))
    report.update(pairing_scope="synthetic-only", live_authorization_created=paired["live_authorization_created"])
    write_private(runtime / "onboarding-report.json", report)
    return report


def stop_child(child):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill(); child.wait(timeout=5)


def pair(instance, args):
    terminal()
    if not args.connect:
        raise Rejected("pair_requires_explicit_connect_to_an_independent_app")
    instance.mutable()
    if importlib.util.find_spec("lark_oapi") is None:
        raise Rejected("install_hash_locked_feishu_sdk_first")
    settings = instance.settings()
    secret = secret_value(read_json(instance.private / "feishu.json")["app_secret"])
    api = FeishuAPI(settings["app_id"], secret, "pairing-discovery")
    pairing, code = instance.begin_pairing(api.bot_info(), timeout=args.timeout)
    print("配对接收仅处理一次性码，不执行工程或调用模型。请确认该应用没有其他接收端。")
    print("在时限内私聊已配置的机器人发送：/pair " + code, flush=True)
    env = {k: v for k, v in os.environ.items() if k in {"SYSTEMROOT", "SystemRoot", "WINDIR", "TEMP", "TMP", "LANG"}}
    env["PATH"] = os.defpath
    env["IM_AGENT_PAIR_CHILD"] = pairing.current()["id"]
    child = subprocess.Popen([sys.executable, "-I", "-B", "-X", "utf8", str(ROOT / "scripts/init_instance.py"),
        "_receive-pair", "--instance-dir", str(instance.root)], env=env, cwd=ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **background_options())
    try:
        while pairing.current()["status"] == "waiting":
            if child.poll() is not None:
                raise Rejected("pairing_receiver_failed_no_authorization")
            time.sleep(.1)
    finally:
        # Stop the pairing receiver before any local decision or future transport.
        stop_child(child)
    candidate = pairing.current()["candidate"]
    print(json.dumps({"verified_candidate": candidate["actor"], "capabilities": project_config(load(instance.root / "instance.json")),
        "model": settings["planner"], "project_source": settings["project_source"]}, ensure_ascii=False, indent=2))
    accepted = input("仅授权上述私聊身份使用这些能力？输入 YES 确认，其他输入拒绝：").strip() == "YES"
    return pairing.approve(accepted)


def receive_pair(instance):
    # Hidden child entry: the parent holds the instance setup lock. No raw-event import CLI exists.
    session = Pairing(instance, "pairing.json").current()
    if session["status"] != "waiting" or os.environ.pop("IM_AGENT_PAIR_CHILD", None) != session["id"]:
        raise Rejected("pairing_not_waiting")
    secret = secret_value(read_json(instance.private / "feishu.json")["app_secret"])
    api = FeishuAPI(session["app_id"], secret, session["bot_open_id"])
    pairing = Pairing(instance, "pairing.json")
    # Even if the interactive parent is killed, this receiver cannot outlive its code.
    watchdog = threading.Timer(max(0, (session["expires_ms"] - time.time() * 1000) / 1000), lambda: os._exit(0))
    watchdog.daemon = True; watchdog.start()
    try:
        with ProcessLock(instance.private / "pair-receiver.lock"):
            receive_events(session["app_id"], secret, lambda raw: pairing.accept(raw, api))
    finally:
        watchdog.cancel()


def prepare_live(instance, args):
    instance.mutable()
    instance.live_config()
    runtime = runtime_path(instance, args.runtime)
    # Fail closed across crashes: never silently create a second fresh message ledger.
    seal = instance.private / "live-runtime.json"
    write_private(seal, {"schema": 1, "runtime": str(runtime), "status": "preparing", "framework_commit": args.commit})
    result = prepare(prepare_args(instance, runtime, args.commit, instance.root / "instance.feishu.json", selected=True))
    settings = instance.settings()
    lines = [SECRET_ENV + "=" + secret_value(read_json(instance.private / "feishu.json")["app_secret"])]
    if settings["planner"]["provider"] == "deepseek":
        lines.append(MODEL_ENV + "=" + secret_value(read_json(instance.private / "model.json")["api_key"]))
    write_private(instance.private / "credentials.env", "\n".join(lines) + "\n")
    write_private(seal, {"schema": 1, "runtime": str(runtime), "status": "prepared", "framework_commit": args.commit})
    result.update(live_runtime_bound=True, credentials_file=str(instance.private / "credentials.env"))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "configure", "demo", "pair", "prepare-live", "status", "_receive-pair"])
    parser.add_argument("--instance-dir", required=True, type=Path)
    parser.add_argument("--id", default="my-twin")
    parser.add_argument("--name", default="我的分身")
    parser.add_argument("--defaults", action="store_true", help="Create a synthetic offline instance without prompts")
    parser.add_argument("--non-interactive", action="store_true", help="Configure only explicitly supplied options; secrets use named environment variables")
    parser.add_argument("--app-id")
    parser.add_argument("--app-secret-env", help="Name of an environment variable, never the secret itself")
    parser.add_argument("--provider", choices=["rules", "deepseek"])
    parser.add_argument("--model")
    parser.add_argument("--model-key-env", help="Name of an environment variable, never the key itself")
    projects = parser.add_mutually_exclusive_group()
    projects.add_argument("--project-repository", type=Path)
    projects.add_argument("--builtin-project", action="store_true")
    parser.add_argument("--project-definition", type=Path)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--commit", help="Full framework SHA; requires clean checkout at that SHA")
    parser.add_argument("--question")
    parser.add_argument("--selected-project", action="store_true", help="Explicitly execute the selected project during offline demo")
    parser.add_argument("--connect", action="store_true", help="Explicitly connect a dedicated Feishu app for bounded pairing")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    if args.command in {"demo", "prepare-live"} and (args.runtime is None or args.commit is None):
        parser.error("demo / prepare-live requires --runtime and --commit")
    if args.command == "pair" and not 30 <= args.timeout <= 600:
        parser.error("pair requires --timeout between 30 and 600 seconds")
    try:
        if args.command == "init":
            if not args.defaults:
                terminal()
            instance = Instance.create(args.instance_dir, ROOT, args.id, args.name)
        else:
            instance = Instance(args.instance_dir, ROOT)
        if args.command == "_receive-pair":
            receive_pair(instance); return 0
        with nullcontext() if args.command == "status" else ProcessLock(instance.private / "setup.lock"):
            if args.command == "configure" or args.command == "init" and not args.defaults:
                result = configure(instance, args)
            elif args.command == "demo":
                result = demo(instance, args)
            elif args.command == "pair":
                result = pair(instance, args)
            elif args.command == "prepare-live":
                result = prepare_live(instance, args)
            else:
                result = instance.status()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        print("init-instance: cancelled; existing instance and evidence preserved", file=sys.stderr)
        return 130
    except (Rejected, CheckError, OSError, ValueError, KeyError, TypeError, EOFError, subprocess.TimeoutExpired) as exc:
        # Only closed error codes may be printed, never file content or SDK responses.
        import re
        code = str(exc) if isinstance(exc, Rejected) and re.fullmatch(r"[a-z_]{1,100}", str(exc)) else "operation_failed"
        print("init-instance: " + code + "; existing instance and evidence preserved", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
