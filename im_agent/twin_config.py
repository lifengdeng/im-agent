"""Validated example-instance configuration; configuration never grants via persona."""
import json
import re
from pathlib import Path

from .adapters import Registry
from .contracts import Rejected, actor, identifier


def load(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > 32768:
        raise Rejected("instance_config_budget")
    return validate(json.loads(path.read_text(encoding="utf-8")))


def validate(value):
    required = {"schema", "profile", "identity", "owner", "persona", "actors", "feishu", "planner"}
    if (not isinstance(value, dict) or not required <= set(value) or set(value) - required - {"project"}
            or type(value["schema"]) is not int or value["schema"] != 1):
        raise Rejected("instance_config_invalid")
    if "project" in value:
        project = value["project"]
        if not isinstance(project, dict) or set(project) != {"id", "adapter", "capabilities"}:
            raise Rejected("instance_project_invalid")
        identifier(project["id"]); identifier(project["adapter"])
        items = project["capabilities"]
        if not isinstance(items, list) or not 1 <= len(items) <= 8:
            raise Rejected("instance_capabilities_invalid")
        names = set()
        for item in items:
            if not isinstance(item, dict) or set(item) != {"name", "description"}:
                raise Rejected("instance_capability_invalid")
            identifier(item["name"])
            if item["name"] in names | {"help", "request"}:
                raise Rejected("instance_capability_reserved_or_duplicate")
            names.add(item["name"])
            if not isinstance(item["description"], str) or not 1 <= len(item["description"]) <= 300:
                raise Rejected("instance_capability_description_required")
    if not isinstance(value["profile"], str) or value["profile"] not in {"offline", "feishu"}:
        raise Rejected("instance_profile_invalid")
    identity = value["identity"]
    if not isinstance(identity, dict) or set(identity) != {"instance", "agent", "tenant"}:
        raise Rejected("instance_identity_invalid")
    actor(dict(identity, user="validate", chat="validate", kind="private"))
    identifier(value["owner"])
    persona = value["persona"]
    if not isinstance(persona, dict) or set(persona) != {"name", "instructions"}:
        raise Rejected("instance_persona_invalid")
    for key, maximum in [("name", 80), ("instructions", 2000)]:
        if not isinstance(persona[key], str) or not 1 <= len(persona[key]) <= maximum:
            raise Rejected("instance_persona_budget")
    if not isinstance(value["actors"], list) or not 1 <= len(value["actors"]) <= 32:
        raise Rejected("instance_actor_policy_required")
    for who in value["actors"]:
        actor(who)
        if any(who[k] != v for k, v in identity.items()):
            raise Rejected("instance_actor_identity_mismatch")
    feishu = value["feishu"]
    keys = {"app_id", "bot_open_id", "app_secret_env"}
    if (not isinstance(feishu, dict) or not keys <= set(feishu)
            or set(feishu) - keys - {"accept_after_ms"}):
        raise Rejected("instance_feishu_invalid")
    identifier(feishu["app_id"]); identifier(feishu["bot_open_id"])
    if not isinstance(feishu["app_secret_env"], str) or not re.fullmatch(r"IM_AGENT_[A-Z0-9_]{1,64}", feishu["app_secret_env"]):
        raise Rejected("instance_secret_reference_required")
    cutoff = feishu.get("accept_after_ms")
    if ((value["profile"] == "feishu" or cutoff is not None)
            and (type(cutoff) is not int or not 0 < cutoff < 10**14)):
        raise Rejected("instance_receive_cutoff_required")
    planner = value["planner"]
    if planner == {"provider": "rules"}:
        return value
    if (not isinstance(planner, dict) or set(planner) != {"provider", "model", "api_key_env"}
            or planner["provider"] != "deepseek" or value["profile"] != "feishu"):
        raise Rejected("instance_planner_invalid")
    identifier(planner["model"])
    if not isinstance(planner["api_key_env"], str) or not re.fullmatch(r"IM_AGENT_[A-Z0-9_]{1,64}", planner["api_key_env"]):
        raise Rejected("instance_secret_reference_required")
    return value


def project_config(config):
    return config.get("project", {"id": "example-project", "adapter": "digital-twin-example-v1",
        "capabilities": [{"name": "inspect-release", "description": "检查示例工程发布记录及风险"}]})


def capability_names(config):
    return [item["name"] for item in project_config(config)["capabilities"]] + ["help"]


def registry(config, snapshot, *, intake=False):
    entries = []
    project = project_config(config)
    for capability in (["request"] if intake else capability_names(config)):
        entries.append({"project": project["id"], "capability": capability,
            "adapter": project["adapter"], "snapshot": snapshot, "actors": config["actors"],
            "input_schema": {"type": "object", "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 1000}},
                             "required": ["text"], "additionalProperties": False},
            "result_schema": {"type": "object", "properties": {
                "verified": {"type": "boolean", "enum": [True]}, "text": {"type": "string", "minLength": 1, "maxLength": 8000},
                "evidence": {"type": "string", "minLength": 64, "maxLength": 64, "enum": [snapshot]}},
                "required": ["verified", "text", "evidence"], "additionalProperties": False}})
    return Registry({"schema": 1, "entries": entries})
