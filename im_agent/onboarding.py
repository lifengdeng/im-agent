"""Independent instance setup. Synthetic pairing never grants live authority."""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time

from .bundles import regular
from .contracts import Rejected, actor, canonical, fingerprint, identifier
from .twin_config import load, validate

MARKER = {"schema": 1, "managed_by": "im-agent-onboarding"}
SECRET_ENV = "IM_AGENT_APP_SECRET"
MODEL_ENV = "IM_AGENT_MODEL_API_KEY"


def read_json(path):
    regular(path)
    if path.stat().st_size > 65536:
        raise Rejected("setup_file_budget")
    return json.loads(path.read_text(encoding="utf-8"))


def write_private(path, value):
    """Atomic replacement; neither a secret nor its temporary file is world readable."""
    regular(path)
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            stream.flush(); os.fsync(stream.fileno())
        regular(path)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def secret_value(value):
    # A closed alphabet also makes the later Compose env file unambiguous.
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_./+=:-]{12,512}", value):
        raise Rejected("credential_format_invalid")
    return value


def independent_path(path, framework):
    path = Path(path).expanduser().absolute(); regular(path)
    path = path.resolve()
    forbidden = [Path(framework).resolve(), Path.home() / ".codex", Path.home() / ".ssh", Path.home() / ".config"]
    if path == Path(path.anchor) or path == Path.home().resolve():
        raise Rejected("instance_root_too_broad")
    if any(path.is_relative_to(other) or other.is_relative_to(path) for other in forbidden):
        raise Rejected("instance_must_be_outside_source_and_credentials")
    if any((parent / ".git").exists() for parent in path.parents):
        raise Rejected("instance_must_not_be_inside_another_repository")
    if any((parent / ".im-agent-instance.json").exists() or parent.name in {".private", ".local"} for parent in path.parents):
        raise Rejected("instance_must_not_be_inside_existing_instance_state")
    return path


class Instance:
    def __init__(self, root, framework):
        self.framework = Path(framework).resolve()
        self.root = independent_path(root, self.framework)
        self.private = self.root / ".private"
        if read_json(self.root / ".im-agent-instance.json") != MARKER:
            raise Rejected("unowned_instance_preserved")
        regular(self.private)

    @classmethod
    def create(cls, root, framework, instance_id="my-twin", name="我的分身"):
        identifier(instance_id)
        root = independent_path(root, framework)
        if root.exists():
            raise Rejected("existing_instance_preserved")
        config = load(Path(framework) / "examples/digital-twin/instance.example.json")
        config["identity"].update(instance=instance_id, agent=instance_id + ".agent")
        config["actors"][0].update(config["identity"])
        config["persona"]["name"] = name
        config["feishu"]["app_secret_env"] = SECRET_ENV
        validate(config)
        root.mkdir(mode=0o700, parents=True)
        if os.name == "nt":
            from scripts.windows_environment import private_acl
            private_acl(root)
        (root / ".private").mkdir(mode=0o700)
        write_private(root / ".im-agent-instance.json", MARKER)
        write_private(root / "instance.json", config)
        write_private(root / ".private/settings.json", {"schema": 1, "revision": secrets.token_hex(16),
            "app_id": None, "planner": {"provider": "rules"}, "project_source": None})
        write_private(root / ".gitignore", ".private/\n.env\n.env.*\n!.env.example\ninstance.feishu.json\nruntime*/\n*.log\n__pycache__/\n")
        write_private(root / ".env.example", "# Secrets are entered by init_instance.py configure, never committed.\nIM_AGENT_APP_SECRET=\nIM_AGENT_MODEL_API_KEY=\n")
        write_private(root / "README.md", "# 独立分身实例\n\ninstance.json 只保存合成离线配置；.private/ 保存凭据、工程路径及配对回执。\n"
            "在 im-agent 仓库中运行 scripts/init_instance.py 的 configure、demo、pair、prepare-live、status；\n"
            "每条命令使用 --instance-dir 指向本目录。详细用法见框架 docs/初始化引导.md。\n\n"
            "project-template 是工程桥接起点。将它复制到独立工程目录，修改、建立 Git 仓库并提交，\n"
            "再通过 configure 选择该工程和 project-template/capabilities.json。不会自动初始化 Git 或启动服务。\n")
        template = root / "project-template"; template.mkdir(mode=0o700)
        for name in ["im_agent_project.py", "project.json"]:
            write_private(template / name, (Path(framework) / "examples/local-project/project" / name).read_text(encoding="utf-8"))
        write_private(template / "capabilities.json", load(Path(framework) / "examples/local-project/instance.example.json")["project"])
        return cls(root, framework)

    def mutable(self):
        regular(self.private / "live-runtime.json")
        if (self.private / "live-runtime.json").exists():
            raise Rejected("live_runtime_binding_preserved_use_instance_maintenance")

    def settings(self):
        value = read_json(self.private / "settings.json")
        if value.get("schema") != 1:
            raise Rejected("setup_schema_invalid")
        return value

    def binding(self):
        items = [load(self.root / "instance.json"), self.settings()]
        for name in ["feishu.json", "model.json"]:
            path = self.private / name; regular(path)
            items.append(read_json(path) if path.exists() else None)
        return fingerprint(items)

    def configure(self, *, app_id=None, app_secret=None, provider=None, model=None, model_key=None, project=None):
        self.mutable()
        config, settings = load(self.root / "instance.json"), self.settings()
        if app_id is not None or app_secret is not None:
            identifier(app_id); secret_value(app_secret)
            settings["app_id"] = app_id
        if provider is not None:
            if provider == "rules":
                settings["planner"] = {"provider": "rules"}
            elif provider == "deepseek":
                identifier(model); secret_value(model_key)
                settings["planner"] = {"provider": "deepseek", "model": model, "api_key_env": MODEL_ENV}
            else:
                raise Rejected("setup_provider_unsupported")
        if project is not None:
            source, declaration = project
            if declaration is None:
                config.pop("project", None)
            else:
                config["project"] = declaration
            settings["project_source"] = source
        preview = copy.deepcopy(config)
        preview["profile"] = "feishu"; preview["feishu"]["accept_after_ms"] = 1
        preview["planner"] = settings["planner"]; validate(preview)
        settings["revision"] = secrets.token_hex(16)
        # A partial write after a crash invalidates the old pairing binding.
        write_private(self.private / "settings.json", settings)
        if app_secret is not None:
            write_private(self.private / "feishu.json", {"app_secret": app_secret})
        if model_key is not None:
            write_private(self.private / "model.json", {"api_key": model_key})
        if provider == "rules" and (self.private / "model.json").exists():
            regular(self.private / "model.json"); (self.private / "model.json").unlink()
        write_private(self.root / "instance.json", config)

    def begin_pairing(self, bot_open_id, *, purpose="feishu", timeout=300, now=None):
        self.mutable()
        if purpose not in {"offline", "feishu"} or not 30 <= timeout <= 600:
            raise Rejected("pairing_session_invalid")
        identifier(bot_open_id)
        settings = self.settings()
        if purpose == "feishu":
            identifier(settings["app_id"])
            secret_value(read_json(self.private / "feishu.json")["app_secret"])
        created = int(time.time() * 1000) if now is None else now
        code = secrets.token_hex(16)
        session = {"schema": 1, "purpose": purpose, "id": secrets.token_hex(16), "binding": self.binding(),
            "created_ms": created, "expires_ms": created + timeout * 1000,
            "code_sha256": hashlib.sha256(code.encode()).hexdigest(), "bot_open_id": bot_open_id,
            "app_id": settings["app_id"] if purpose == "feishu" else "example-app", "status": "waiting"}
        name = "pairing.json" if purpose == "feishu" else "offline-pairing.json"
        write_private(self.private / name, session)
        return Pairing(self, name), code

    def live_config(self):
        receipt = read_json(self.private / "pairing.json")
        config = load(self.root / "instance.feishu.json")
        if (receipt.get("purpose") != "feishu" or receipt.get("status") != "approved"
                or receipt.get("binding") != self.binding() or receipt.get("config_sha256") != fingerprint(config)):
            raise Rejected("real_pairing_required_or_configuration_changed")
        return config

    def status(self):
        def exists(name):
            regular(self.private / name)
            return (self.private / name).is_file()
        approved = False
        if exists("pairing.json") and (self.root / "instance.feishu.json").exists():
            try:
                self.live_config(); approved = True
            except Rejected:
                pass
        return {"instance": load(self.root / "instance.json")["identity"]["instance"], "default_profile": "offline",
            "feishu_configured": exists("feishu.json"), "model_provider": self.settings()["planner"]["provider"],
            "model_credentials_configured": exists("model.json"), "model_connection_verified": False,
            "project_selected": self.settings()["project_source"] is not None,
            "live_pairing_approved": approved, "live_runtime_bound": exists("live-runtime.json"), "services_started": False}


class Pairing:
    """Only an authenticated SDK callback may call accept() for a live session."""
    def __init__(self, instance, filename):
        self.instance = instance
        if filename not in {"pairing.json", "offline-pairing.json"}:
            raise Rejected("pairing_file_invalid")
        self.path = instance.private / filename
        self.lock = threading.Lock()

    def current(self, now=None):
        value = read_json(self.path)
        now = int(time.time() * 1000) if now is None else now
        expected = "feishu" if self.path.name == "pairing.json" else "offline"
        if (value.get("purpose") != expected or value.get("binding") != self.instance.binding()
                or not value["created_ms"] <= now < value["expires_ms"]):
            raise Rejected("pairing_expired_or_configuration_changed")
        return value

    def accept(self, raw, api, *, now=None):
        with self.lock:
            session = self.current(now)
            if session["status"] != "waiting":
                return False
            try:
                header, body = raw["header"], raw["event"]
                message, sender = body["message"], body["sender"]
                tenant = identifier(header["tenant_key"])
                created = message["create_time"]
                text = json.loads(message["content"])["text"]
                if (raw["schema"] != "2.0" or header["event_type"] != "im.message.receive_v1"
                        or header["app_id"] != session["app_id"] or api.app_id != session["app_id"]
                        or sender["sender_type"] != "user" or sender["tenant_key"] != tenant
                        or message["chat_type"] != "p2p" or message["message_type"] != "text"
                        or not isinstance(created, str) or not re.fullmatch(r"[0-9]{1,14}", created)
                        or not session["created_ms"] <= int(created) < session["expires_ms"]
                        or not isinstance(text, str) or not re.fullmatch(r"/pair [a-f0-9]{32}", text)
                        or not secrets.compare_digest(hashlib.sha256(text[6:].encode()).hexdigest(), session["code_sha256"])):
                    return False
                who = actor(dict(load(self.instance.root / "instance.json")["identity"], tenant=tenant,
                    user=sender["sender_id"]["open_id"], chat=message["chat_id"], kind="private"))
                returned = api.message(message["message_id"])
                if (returned.get("chat_id") != who["chat"] or returned.get("msg_type") != "text"
                        or returned.get("sender", {}).get("id") != who["user"]
                        or returned.get("sender", {}).get("sender_type") != "user"
                        or json.loads(returned["body"]["content"]) != {"text": text}):
                    return False
                session.update(status="candidate", candidate={"actor": who, "message_id": message["message_id"],
                    "readback_sha256": fingerprint(returned)})
                write_private(self.path, session)
                return True
            except (KeyError, TypeError, ValueError):
                return False

    def approve(self, accepted, *, now=None):
        self.instance.mutable()
        value = self.current(now)
        if value["status"] != "candidate":
            raise Rejected("pairing_candidate_required")
        value["status"] = "approved" if accepted else "denied"
        if accepted and value["purpose"] == "feishu":
            config = load(self.instance.root / "instance.json")
            who = actor(value["candidate"]["actor"])
            config.update(profile="feishu", owner=who["user"], actors=[who])
            config["identity"] = {k: who[k] for k in ["instance", "agent", "tenant"]}
            config["feishu"] = {"app_id": value["app_id"], "bot_open_id": value["bot_open_id"],
                "app_secret_env": SECRET_ENV, "accept_after_ms": int(time.time() * 1000) if now is None else now}
            config["planner"] = self.instance.settings()["planner"]
            validate(config)
            value["config_sha256"] = fingerprint(config)
            # Receipt is committed last; an interrupted write is never an approval.
            write_private(self.instance.root / "instance.feishu.json", config)
        write_private(self.path, value)
        return {"approved": accepted, "purpose": value["purpose"], "live_authorization_created": accepted and value["purpose"] == "feishu"}
