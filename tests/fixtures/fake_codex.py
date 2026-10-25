"""Synthetic stdio peer. No network, auth, model, or project access."""
import json
import sys

mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
thread, turn = "synthetic-thread", "synthetic-turn"


def send(value):
    print(json.dumps(value), flush=True)


def complete(status="completed"):
    send({"method": "item/completed", "params": {"threadId": thread, "turnId": turn,
        "item": {"type": "agentMessage", "text": '{"answer":"synthetic"}'}}})
    send({"method": "turn/completed", "params": {"threadId": thread, "turn": {"id": turn, "status": status}}})


for line in sys.stdin:
    value = json.loads(line)
    if "method" not in value: continue
    method = value["method"]
    if method == "initialized": continue
    result = {}
    if method == "initialize": result = {"platformOs": "synthetic"}
    elif method in {"thread/start", "thread/resume"}:
        result = {"thread": {"id": "wrong-thread" if mode == "wrong-thread" else thread}}
    elif method == "turn/start": result = {"turn": {"id": turn}}
    send({"id": value["id"], "result": result})
    if method == "turn/start":
        send({"id": "synthetic-approval", "method": "item/commandExecution/requestApproval", "params": {}})
        if mode == "normal": complete()
    elif method == "turn/steer": complete()
    elif method == "turn/interrupt": complete("interrupted")
