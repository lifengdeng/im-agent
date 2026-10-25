"""Observe console SHOW events in the logged-on desktop; retain no window text.

Run using pythonw in the same interactive session as the resident. Existing
windows are preserved. New terminal windows are reported without closing them;
if the user opens an unrelated terminal during the check, inspect that result.
"""
import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
from im_agent.resident import atomic_json
from windows_environment import reject_links


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=180)
    args = parser.parse_args()
    if os.name != "nt" or not 30 <= args.seconds <= 300:
        parser.error("interactive Windows session and 30..300 seconds required")
    runtime = args.runtime_root.resolve(); reject_links(runtime)
    if json.loads((runtime / "instance.json").read_text()) != {"schema": 1, "profile": "isolated-codex"}:
        raise ValueError("prepared_runtime_required")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    session = wintypes.DWORD()
    if not kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)) or session.value == 0:
        raise ValueError("desktop_session_required_ssh_session_cannot_observe_windows")
    user = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
                                      wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)
    enum_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, callback_type,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
    user.SetWinEventHook.restype = wintypes.HANDLE
    user.UnhookWinEvent.argtypes = [wintypes.HANDLE]
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.EnumWindows.argtypes = [enum_type, wintypes.LPARAM]
    user.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
    consoles = {"ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS"}
    baseline, found = set(), {}
    def terminal(hwnd):
        name = ctypes.create_unicode_buffer(256)
        user.GetClassNameW(hwnd, name, len(name))
        if name.value not in consoles: return None
        pid = wintypes.DWORD(); user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return {"pid": pid.value, "class": name.value}
    def existing(hwnd, _):
        if user.IsWindowVisible(hwnd) and terminal(hwnd): baseline.add(int(hwnd))
        return True
    enumeration = enum_type(existing)
    user.EnumWindows(enumeration, 0)
    def shown(hook, event, hwnd, object_id, child, thread, stamp):
        if not hwnd or object_id != 0 or child != 0 or int(hwnd) in baseline: return
        value = terminal(hwnd)
        if value: found[int(hwnd)] = value
    callback = callback_type(shown)
    hook = user.SetWinEventHook(0x8002, 0x8002, None, callback, 0, 0, 0)
    if not hook: raise ctypes.WinError(ctypes.get_last_error())
    path = runtime / "resident/window-check.json"
    started = time.time()
    value = {"schema": 1, "session": session.value, "pid": os.getpid(), "started": started,
             "seconds": args.seconds, "existing_visible_terminals": len(baseline), "window_text_collected": False}
    atomic_json(path, dict(value, status="observing"))
    deadline = time.monotonic() + args.seconds
    try:
        message = wintypes.MSG()
        while time.monotonic() < deadline:
            while user.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                user.TranslateMessage(ctypes.byref(message)); user.DispatchMessageW(ctypes.byref(message))
            time.sleep(.02)
    finally:
        user.UnhookWinEvent(hook)
        atomic_json(path, dict(value, status="passed" if not found else "new_terminal_observed",
                              finished=time.time(), new_terminal_windows=list(found.values())))


if __name__ == "__main__":
    main()
