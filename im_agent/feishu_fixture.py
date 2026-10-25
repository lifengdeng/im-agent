"""Loopback Feishu API fixture: the real codec, routing and read-back path run unchanged."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

from .contracts import canonical


class Fixture:
    def __init__(self, raw, config):
        message, sender = raw["event"]["message"], raw["event"]["sender"]
        self.original = {"message_id": message["message_id"], "chat_id": message["chat_id"], "msg_type": "text",
                         "sender": {"id": sender["sender_id"]["open_id"], "sender_type": "user"}, "body": {"content": message["content"]}}
        self.replies, self.uuids = {}, {}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *unused):
                pass

            def respond(self, value):
                raw = canonical(value).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 32768:
                    self.send_error(400); return
                payload = json.loads(self.rfile.read(length))
                if self.path == "/auth/v3/tenant_access_token/internal":
                    self.respond({"code": 0, "tenant_access_token": "fixture-token", "expire": 7200}); return
                if self.headers.get("Authorization") != "Bearer fixture-token" or self.path != "/im/v1/messages/" + message["message_id"] + "/reply":
                    self.send_error(403); return
                uid = payload["uuid"]
                if uid not in owner.uuids:
                    mid = "om_example_reply_" + str(len(owner.replies) + 1)
                    owner.uuids[uid] = mid
                    owner.replies[mid] = {"message_id": mid, "chat_id": message["chat_id"], "msg_type": payload["msg_type"],
                        "sender": {"id": config["feishu"]["app_id"], "sender_type": "app"},
                        "parent_id": message["message_id"], "body": {"content": payload["content"]}}
                self.respond({"code": 0, "data": {"message_id": owner.uuids[uid]}})

            def do_GET(self):
                if self.headers.get("Authorization") != "Bearer fixture-token":
                    self.send_error(403); return
                if self.path == "/bot/v3/info":
                    self.respond({"code": 0, "bot": {"activate_status": 2, "open_id": config["feishu"]["bot_open_id"]}}); return
                mid = self.path.removeprefix("/im/v1/messages/")
                item = owner.original if mid == message["message_id"] else owner.replies.get(mid)
                if item is None:
                    self.send_error(404); return
                self.respond({"code": 0, "data": {"items": [item]}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return "http://127.0.0.1:" + str(self.server.server_port)

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)
