"""Feishu text intake and read-back-verified replies for an isolated instance."""
import json
import re
import time
import urllib.request

from .contracts import Rejected, canonical, fingerprint, identifier
from .delivery import validate_platform_id
from .service import NoRedirect
from .twin_config import project_config


def normalize(raw, config):
    """Call only from an authenticated SDK connection (or a synthetic fixture)."""
    try:
        header, body = raw["header"], raw["event"]
        if (raw.get("schema") != "2.0" or header["event_type"] != "im.message.receive_v1"
                or header["app_id"] != config["feishu"]["app_id"]
                or header["tenant_key"] != config["identity"]["tenant"]):
            raise Rejected("feishu_event_identity_rejected")
        message, sender = body["message"], body["sender"]
        if config["profile"] == "feishu":
            created = message.get("create_time")
            if (not isinstance(created, str) or not re.fullmatch(r"[0-9]{1,14}", created)
                    or int(created) < config["feishu"]["accept_after_ms"]):
                raise Rejected("feishu_message_before_instance_cutoff")
        if sender["sender_type"] != "user" or message["message_type"] != "text":
            raise Rejected("feishu_text_user_required")
        if sender.get("tenant_key", header["tenant_key"]) != header["tenant_key"]:
            raise Rejected("feishu_sender_tenant_rejected")
        kind = {"p2p": "private", "group": "group"}.get(message["chat_type"])
        if kind is None:
            raise Rejected("feishu_chat_type_rejected")
        who = dict(config["identity"], user=sender["sender_id"]["open_id"], chat=message["chat_id"], kind=kind)
        if who not in config["actors"]:
            raise Rejected("feishu_actor_rejected")
        identifier(message["message_id"])
        if not re.fullmatch(r"om_[A-Za-z0-9_-]{1,120}", message["message_id"]):
            raise Rejected("feishu_message_id_rejected")
        text = json.loads(message["content"])["text"]
        if not isinstance(text, str):
            raise Rejected("feishu_text_required")
        if kind == "group":
            mentions = [m for m in message.get("mentions", []) if m.get("id", {}).get("open_id") == config["feishu"]["bot_open_id"]]
            if not mentions or any(not isinstance(m.get("key"), str) or not m["key"] for m in mentions):
                raise Rejected("feishu_bot_mention_required")
            for mention in mentions:
                text = text.replace(mention["key"], "")
        text = text.strip()
        if not 1 <= len(text) <= 1000:
            raise Rejected("feishu_text_budget")
        return {"id": message["message_id"], "actor": who, "project": project_config(config)["id"], "capability": "request", "input": {"text": text}}
    except (ValueError, TypeError, KeyError):
        raise Rejected("feishu_event_rejected") from None


class FeishuAPI:
    def __init__(self, app_id, secret, bot_open_id, *, fixture_base=None):
        self.app_id, self.secret, self.bot_open_id = app_id, secret, bot_open_id
        self.base = "https://open.feishu.cn/open-apis"
        if fixture_base is not None:
            from urllib.parse import urlsplit
            parsed = urlsplit(fixture_base)
            if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path:
                raise Rejected("fixture_address_rejected")
            self.base = fixture_base
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.token, self.expires = "", 0
        self.sends = 0

    def request(self, method, path, payload=None, *, authenticated=True):
        if authenticated and time.monotonic() >= self.expires:
            token = self.request("POST", "/auth/v3/tenant_access_token/internal", {"app_id": self.app_id, "app_secret": self.secret}, authenticated=False)
            if not isinstance(token.get("tenant_access_token"), str) or not isinstance(token.get("expire"), int) or token["expire"] <= 60:
                raise Rejected("feishu_token_response_invalid")
            self.token, self.expires = token["tenant_access_token"], time.monotonic() + token["expire"] - 60
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if authenticated:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(self.base + path, method=method,
                  data=canonical(payload).encode() if payload is not None else None, headers=headers)
        try:
            with self.opener.open(request, timeout=15) as response:
                raw = response.read(131073)
            if len(raw) > 131072:
                raise ValueError("budget")
            result = json.loads(raw)
            if not isinstance(result, dict) or result.get("code") != 0:
                raise ValueError("api")
            return result
        except (OSError, ValueError, TypeError):
            # A failed POST may already have succeeded. Never automatically retry.
            raise Rejected("feishu_request_unknown_no_replay") from None

    def message(self, message_id):
        validate_platform_id("feishu", "", message_id)
        response = self.request("GET", "/im/v1/messages/" + message_id)
        try:
            items = response["data"]["items"]
            if len(items) != 1 or items[0]["message_id"] != message_id or items[0].get("deleted") is True:
                raise ValueError("message")
            return items[0]
        except (KeyError, TypeError, ValueError):
            raise Rejected("feishu_readback_invalid") from None

    def bot_info(self):
        """Discover the configured app's own bot, without sending a message."""
        response = self.request("GET", "/bot/v3/info")
        try:
            bot = response["bot"]
            if bot["activate_status"] != 2:
                raise ValueError("inactive")
            return identifier(bot["open_id"])
        except (KeyError, TypeError, ValueError):
            raise Rejected("feishu_active_bot_required") from None

    def reply(self, intent):
        original = self.message(intent["id"])
        who = intent["actor"]
        if (original.get("chat_id") != who["chat"] or original.get("sender", {}).get("id") != who["user"]
                or original.get("sender", {}).get("sender_type") != "user"):
            raise Rejected("feishu_reply_anchor_rejected")
        text = intent["result"]["text"] + "\n证据摘要：" + intent["result"]["evidence"][:12]
        body = {"msg_type": "text", "content": canonical({"text": text}), "uuid": fingerprint([self.app_id, intent])[:32]}
        self.sends += 1
        response = self.request("POST", "/im/v1/messages/" + intent["id"] + "/reply", body)
        try:
            mid = response["data"]["message_id"]
            returned = self.message(mid)
            if (returned.get("chat_id") != who["chat"] or returned.get("msg_type") != "text"
                    or returned.get("sender", {}).get("sender_type") != "app"
                    or returned.get("sender", {}).get("id") not in {self.app_id, self.bot_open_id}
                    or json.loads(returned["body"]["content"]) != {"text": text}):
                raise ValueError("readback")
            # Parent/root IDs, when present, must include the originating message.
            anchors = {returned.get("parent_id"), returned.get("root_id")} - {None, ""}
            if anchors and intent["id"] not in anchors:
                raise ValueError("anchor")
            return mid
        except (KeyError, TypeError, ValueError):
            raise Rejected("feishu_reply_unverified_no_replay") from None


def receive(config, accept, secret):
    """Official SDK owns authentication and event ACK; callback only persists intake."""
    def received(raw):
        try:
            value = normalize(raw, config)
        except Rejected:
            return  # Ignore unauthorized/irrelevant events without starting work.
        # Persistence failures propagate: never ACK accepted work before storage.
        accept(value)

    receive_events(config["feishu"]["app_id"], secret, received)


def receive_events(app_id, secret, accept):
    """Authenticated raw SDK events, used only by intake and bounded local pairing."""
    import lark_oapi as lark
    from lark_oapi.core.log import logger
    logger.disabled = True  # SDK diagnostics can contain signed connection URLs.

    def received(data):
        accept(json.loads(lark.JSON.marshal(data)))

    handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(received).build()
    try:
        lark.ws.Client(app_id, secret, event_handler=handler, log_level=lark.LogLevel.ERROR).start()
    except Exception:
        raise Rejected("feishu_connection_failed") from None
