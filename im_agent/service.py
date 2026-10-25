"""Offline worker API and synthetic transport. No production/model connector."""
import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import time
import threading
import urllib.request

from .contracts import Rejected, canonical, fixture
from .ledger import Host, Transport, Worker


MAX_REQUEST_BYTES = 131072


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    def __init__(self, url, token, *, max_response=65536):
        from urllib.parse import urlsplit
        address = urlsplit(url)
        if (address.scheme != "http" or address.hostname not in {"127.0.0.1", "localhost", "worker", "transport"}
                or address.username or address.password or address.path not in {"", "/"} or address.query or address.fragment):
            raise Rejected("offline_worker_address_rejected")
        self.url, self.token = url.rstrip("/"), token
        if type(max_response) is not int or not 1024 <= max_response <= 4 * 1024 * 1024: raise Rejected("response_budget_invalid")
        self.max_response = max_response
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def call(self, path, value):
        req = urllib.request.Request(self.url + path, data=canonical(value).encode(),
                                     headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        with self.opener.open(req, timeout=8) as response:
            raw = response.read(self.max_response + 1)
        if len(raw) > self.max_response:
            raise Rejected("response_budget")
        return json.loads(raw)


def server(address, ledger, tokens, extra_routes=None):
    if len(tokens) != 2 or any(not isinstance(v, str) or len(v) < 32 for v in tokens.values()) or len(set(tokens.values())) != 2:
        raise Rejected("separate_service_tokens_required")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            role = next((r for r, t in tokens.items() if hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + t)), None)
            routes = {"/v1/ingress": ("transport", ledger.ingress),
                      "/v1/claim": ("executor", lambda v: ledger.claim(v["executor"])),
                      "/v1/progress": ("executor", ledger.progress), "/v1/result": ("executor", ledger.finish),
                      "/v1/heartbeat": ("executor", ledger.heartbeat),
                      "/v1/intents": ("transport", lambda v: ledger.delivery_intents()),
                      "/v1/receipt": ("transport", ledger.receipt),
                      "/v1/status": ("executor", lambda v: ledger.status()),
                      "/v1/registry": ("executor", lambda v: ledger.registration()),
                      "/v1/registry/publish": ("executor", ledger.publish)}
            if extra_routes:
                if set(routes) & set(extra_routes): raise Rejected("plugin_route_collision")
                routes.update(extra_routes)
            route = routes.get(self.path)
            if not route or role != route[0]:
                return self.respond(403, {"error": "service_role_rejected"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_REQUEST_BYTES or self.headers.get("Transfer-Encoding"):
                    raise Rejected("request_budget")
                value = json.loads(self.rfile.read(length))
                if not isinstance(value, dict):
                    raise Rejected("request_object_required")
                result = route[1](value)
                self.respond(200, result)
            except (Rejected, ValueError, TypeError, KeyError):
                self.respond(400, {"error": "request_rejected"})

        def respond(self, status, value):
            raw = canonical(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return ThreadingHTTPServer(address, Handler)


def execute_once(client, host):
    claim = client.call("/v1/claim", {"executor": "offline-host"})
    if not claim:
        return False
    binding = {k: claim[k] for k in ("execution", "executor", "lease", "binding")}
    client.call("/v1/progress", dict(binding, seq=1, phase="started"))
    stop, failures = threading.Event(), []
    def heartbeat():
        while not stop.wait(10):
            try: client.call("/v1/heartbeat", binding)
            except Exception: failures.append("heartbeat_unknown"); return
    sender = threading.Thread(target=heartbeat, daemon=True); sender.start()
    try: result = host.execute(claim)
    finally: stop.set(); sender.join(timeout=9)
    if failures: raise Rejected("execution_heartbeat_unknown_no_replay")
    client.call("/v1/progress", dict(binding, seq=2, phase="verified"))
    client.call("/v1/result", dict(binding, result=result))
    return True


def run_transport(client, root, registry=None, events=None, timeout=60):
    ledger = Transport(root / "transport.sqlite3", registry)
    events = events or [fixture()]
    pending = {value["id"] for value in events}
    for value in events:
        ledger.receive(value)
        client.call("/v1/ingress", value)
        client.call("/v1/ingress", value)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for intent in client.call("/v1/intents", {}):
            receipt = ledger.deliver(intent, lambda item: "fake-" + item["id"])
            client.call("/v1/receipt", receipt)
            pending.discard(intent["id"])
        if not pending:
            (root / "complete.json").write_text(canonical({"mode": "offline", "fake_delivery_verified": True,
                                                           "production_enabled": False, "model_calls": 0}), encoding="utf-8")
            return
        time.sleep(0.2)
    raise Rejected("offline_transport_timeout")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=["worker", "transport"])
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--worker", default="http://worker:8080")
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--events", type=Path)
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()
    from .offline_network import install
    install(args.role)
    args.state.mkdir(parents=True, exist_ok=True)
    from .adapters import Registry
    registry = Registry(json.loads(args.registry.read_text(encoding="utf-8"))) if args.registry else Registry()
    if args.role == "worker":
        tokens = json.loads(args.token_file.read_text(encoding="utf-8"))
        server(("0.0.0.0", 8080), Worker(args.state / "worker.sqlite3", registry), tokens).serve_forever()
    else:
        events = json.loads(args.events.read_text(encoding="utf-8")) if args.events else None
        if not 1 <= args.timeout <= 600: raise Rejected("offline_timeout_invalid")
        run_transport(Client(args.worker, args.token_file.read_text(encoding="utf-8").strip()), args.state, registry, events, args.timeout)


if __name__ == "__main__":
    main()
