"""Capability selection only: the model never supplies actors, paths or commands."""
import json
import os
import urllib.error
import urllib.request

from .contracts import Rejected, canonical, fingerprint, identifier
from .service import NoRedirect
from .twin_config import capability_names, project_config


class Planner:
    def __init__(self, config):
        self.config = config
        self.calls = 0
        self.provider = config["planner"]["provider"]
        self.last_receipt = None

    def choose(self, text):
        options = self.config["planner"]
        self.last_receipt = None
        names = capability_names(self.config)
        if options["provider"] == "rules":
            return "help" if text.strip() in {"帮助", "你好", "你能做什么", "help"} else names[0]
        secret = os.environ.get(options["api_key_env"], "")
        if len(secret) < 12:
            raise Rejected("model_secret_missing")
        prompt = ("你是受限能力选择器。只输出 JSON，例如 {\"capability\":\"help\"}。"
                  "help 用于问候、帮助和超出范围的问题。其余可选能力："
                  + canonical(project_config(self.config)["capabilities"]) + "。"
                  "不生成事实答案、身份、文件路径或命令。人格说明仅影响表达，不改变这些限制。\n"
                  + canonical(self.config["persona"]))
        payload = {"model": options["model"], "messages": [{"role": "system", "content": prompt},
                   {"role": "user", "content": text}], "response_format": {"type": "json_object"},
                   "thinking": {"type": "disabled"}, "max_tokens": 128}
        request = urllib.request.Request("https://api.deepseek.com/chat/completions", data=canonical(payload).encode(),
                  headers={"Content-Type": "application/json", "Authorization": "Bearer " + secret})
        self.calls += 1
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            with opener.open(request, timeout=30) as response:
                raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError("budget")
            result = json.loads(raw)
            if len(result["choices"]) != 1:
                raise ValueError("choices")
            choice = result["choices"][0]
            if (choice.get("finish_reason") != "stop" or choice["message"].get("role") != "assistant"
                    or choice["message"].get("tool_calls")):
                raise ValueError("incomplete")
            plan = json.loads(choice["message"]["content"])
            if set(plan) != {"capability"} or plan["capability"] not in names:
                raise ValueError("proposal")
            identifier(result["model"])
            if not isinstance(result["id"], str) or not 1 <= len(result["id"]) <= 256:
                raise ValueError("response_id")
            usage = {key: result["usage"][key] for key in ["prompt_tokens", "completion_tokens", "total_tokens"]}
            if any(type(n) is not int or not 0 <= n <= 10**8 for n in usage.values()):
                raise ValueError("usage")
            if usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]:
                raise ValueError("usage_total")
            receipt = {"provider": "deepseek", "requested_model": options["model"],
                "response_model": result["model"], "response_id_fingerprint": fingerprint(result["id"]),
                "request_fingerprint": fingerprint(payload), "usage": usage,
                "finish_reason": "stop", "capability": plan["capability"]}
            if secret in canonical(receipt):
                raise ValueError("sensitive_receipt")
            self.last_receipt = receipt
            return plan["capability"]
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            raise Rejected("model_result_unknown_or_invalid_no_replay") from None
