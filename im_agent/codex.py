"""Bounded Codex stdio client with explicit inference opt-in and owned processes."""
import json
import os
import queue
import signal
import subprocess
import threading
import time

from .adapters import validate
from .contracts import Rejected
from .processes import background_options


class ProcessTree:
    def __init__(self, process):
        self.process, self.job = process, None
        if os.name != "nt": return
        import ctypes
        from ctypes import wintypes
        class Basic(ctypes.Structure):
            _fields_ = [("user", ctypes.c_int64), ("job_user", ctypes.c_int64), ("flags", wintypes.DWORD),
                        ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t), ("active", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", ctypes.c_uint64 * 6), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel = kernel
        self.job = kernel.CreateJobObjectW(None, None)
        limits = Extended(); limits.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE
        if (not self.job or not kernel.SetInformationJobObject(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                or not kernel.AssignProcessToJobObject(self.job, wintypes.HANDLE(int(process._handle)))):
            if self.job: kernel.CloseHandle(self.job)
            self.job = None
            process.kill(); process.wait(timeout=5)
            raise Rejected("windows_process_job_unavailable")

    def close(self):
        if self.job is not None:
            self.kernel.CloseHandle(self.job); self.job = None
        elif os.name != "nt":
            try: os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError: pass


class Rpc:
    def __init__(self, command, env, cwd, timeout=30):
        self.deadline = time.monotonic() + timeout
        self.messages, self.events = queue.Queue(maxsize=256), []
        self.fault = None; self.sequence = 0; self.closed = False
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        env=env, cwd=cwd, start_new_session=os.name != "nt", **background_options())
        self.tree = ProcessTree(self.process)
        self.readers = []
        for stream, stdout in [(self.process.stdout, True), (self.process.stderr, False)]:
            reader = threading.Thread(target=self._read, args=(stream, stdout), daemon=True)
            reader.start(); self.readers.append(reader)

    def _read(self, stream, stdout):
        try:
            while True:
                line = stream.readline(1048577)
                if not stdout:
                    if not line: return
                    continue  # Never retain credential-bearing stderr.
                if not line:
                    value = None
                elif len(line) > 1048576:
                    self.fault = "codex_protocol_budget"; return
                else:
                    try: value = json.loads(line)
                    except (ValueError, UnicodeError): self.fault = "codex_protocol_invalid"; return
                try: self.messages.put_nowait(value)
                except queue.Full: self.fault = "codex_protocol_budget"; return
                if value is None: return
        except (OSError, ValueError):
            return

    def send(self, value):
        raw = (json.dumps(value, ensure_ascii=False) + "\n").encode()
        if len(raw) > 131072: raise Rejected("codex_request_budget")
        try: self.process.stdin.write(raw); self.process.stdin.flush()
        except (OSError, ValueError): raise Rejected("codex_connection_unknown") from None

    def receive(self):
        while True:
            if self.fault: raise Rejected(self.fault)
            if time.monotonic() >= self.deadline: raise Rejected("codex_timeout_unknown")
            try: value = self.messages.get(timeout=0.1)
            except queue.Empty: continue
            if value is None: raise Rejected("codex_connection_unknown")
            if not isinstance(value, dict): raise Rejected("codex_protocol_invalid")
            if "method" in value and "id" in value:
                self.send({"id": value["id"], "error": {"code": -32601, "message": "Interactive action not authorized"}})
                continue
            return value

    def call(self, method, params):
        self.sequence += 1; identifier = self.sequence
        self.send({"id": identifier, "method": method, "params": params})
        while True:
            value = self.receive()
            if "id" in value:
                if value["id"] != identifier: raise Rejected("codex_response_id_mismatch")
                if "error" in value: raise Rejected("codex_request_rejected")
                return value.get("result", {})
            self.events.append(value)
            if len(self.events) > 256: raise Rejected("codex_event_budget")

    def event(self):
        return self.events.pop(0) if self.events else self.receive()

    def close(self):
        if self.closed: return
        self.closed = True
        try:
            if not self.process.stdin.closed: self.process.stdin.close()
            try: self.process.wait(timeout=2)
            except subprocess.TimeoutExpired: pass
        finally:
            self.tree.close()
            if self.process.poll() is None:
                self.process.kill(); self.process.wait(timeout=5)
            for reader in self.readers: reader.join(timeout=2)
            self.process.stdout.close(); self.process.stderr.close()


class Session:
    def __init__(self, command, env, cwd, *, allow_inference=False, timeout=30):
        self.rpc = Rpc(command, env, cwd, timeout)
        self.allow_inference, self.initialized = allow_inference, False
        self.cwd, self.thread, self.turn = str(cwd), None, None

    def initialize(self):
        if self.initialized: raise Rejected("codex_already_initialized")
        value = self.rpc.call("initialize", {"clientInfo": {"name": "im_agent", "title": "IM Agent", "version": "0.2.0"}})
        self.rpc.send({"method": "initialized", "params": {}})
        self.initialized = True
        return {"initialized": True, "platform_os": value.get("platformOs"), "model_turns": 0}

    def start(self, prompt, schema, *, thread=None):
        if not self.initialized: raise Rejected("codex_not_initialized")
        if not self.allow_inference: raise Rejected("inference_not_enabled")
        if self.turn: raise Rejected("codex_turn_already_active")
        if not isinstance(prompt, str) or not 1 <= len(prompt) <= 16000: raise Rejected("codex_input_budget")
        params = {"cwd": self.cwd, "approvalPolicy": "never", "config": {"sandbox_mode": "read-only", "web_search": "disabled"}}
        if thread: params.update(threadId=thread, excludeTurns=True)
        response = self.rpc.call("thread/resume" if thread else "thread/start", params)
        self.thread = response.get("thread", {}).get("id")
        if not isinstance(self.thread, str) or not self.thread or thread and self.thread != thread:
            raise Rejected("codex_thread_binding_changed")
        response = self.rpc.call("turn/start", {"threadId": self.thread, "input": [{"type": "text", "text": prompt}],
                                                "approvalPolicy": "never", "outputSchema": schema})
        self.turn = response.get("turn", {}).get("id")
        if not isinstance(self.turn, str) or not self.turn: raise Rejected("codex_turn_missing")
        self.schema = schema
        return {"thread": self.thread, "turn": self.turn}

    def steer(self, text):
        if not self.turn or not isinstance(text, str) or not 1 <= len(text) <= 8000: raise Rejected("codex_control_invalid")
        return self.rpc.call("turn/steer", {"threadId": self.thread, "expectedTurnId": self.turn, "input": [{"type": "text", "text": text}]})

    def interrupt(self):
        if not self.turn: raise Rejected("codex_control_invalid")
        return self.rpc.call("turn/interrupt", {"threadId": self.thread, "turnId": self.turn})

    def wait(self):
        if not self.turn: raise Rejected("codex_no_active_turn")
        answer, budget = None, 0
        while True:
            value = self.rpc.event(); budget += 1
            if budget > 2000: raise Rejected("codex_event_budget")
            method, params = value.get("method"), value.get("params", {})
            if not isinstance(params, dict) or params.get("threadId") != self.thread: continue
            if method == "turn/completed":
                turn = params.get("turn", {})
                if turn.get("id") != self.turn: continue
                self.turn = None
                if turn.get("status") != "completed": raise Rejected("codex_turn_" + ("interrupted" if turn.get("status") == "interrupted" else "failed"))
                if not isinstance(answer, str) or len(answer.encode()) > 16384: raise Rejected("codex_result_missing_or_large")
                try: result = json.loads(answer)
                except ValueError: raise Rejected("codex_result_invalid") from None
                validate(result, self.schema)
                return result
            if params.get("turnId") != self.turn: continue
            if method == "item/completed" and params.get("item", {}).get("type") == "agentMessage":
                answer = params["item"].get("text")

    def close(self): self.rpc.close()
