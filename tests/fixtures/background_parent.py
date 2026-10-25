"""A real pythonw parent exercises CLI pipes without inheriting a console."""
import ctypes
import json
from pathlib import Path
import subprocess
import sys

root, report, child = map(Path, sys.argv[1:])
sys.path[:0] = [str(root), str(root / "scripts")]
from im_agent.processes import background_options
from windows_environment import command
from offline_pc import run

probe = "import ctypes,json,sys; print(json.dumps({'console':int(ctypes.windll.kernel32.GetConsoleWindow()),'echo':sys.stdin.readline().strip() if sys.stdin else ''}))"
args = [str(child), "-B", "-c", probe]
try:
    result = subprocess.run(args, input="pipe-roundtrip\n", capture_output=True, text=True,
                            check=True, timeout=10, **background_options())
    args = [str(child), "-B", "-c", "import ctypes,json; print(json.dumps({'console':int(ctypes.windll.kernel32.GetConsoleWindow())}))"]
    values = [json.loads(result.stdout), json.loads(command(args).stdout), json.loads(run(args))]
    report.write_text(json.dumps({"parent_console": int(ctypes.windll.kernel32.GetConsoleWindow()),
                                  "children": values}), encoding="utf-8")
except Exception as exc:
    report.write_text(json.dumps({"error": type(exc).__name__}), encoding="utf-8")
    raise
