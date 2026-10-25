"""Run one reviewed example program from a hash-verified release, without a shell."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from .bundles import inventory
from .contracts import Rejected, fingerprint
from .processes import background_options


class Project:
    def __init__(self, root, snapshot, persona, declaration=None):
        self.root, self.snapshot, self.persona = Path(root), snapshot, persona
        self.declaration = declaration

    def execute(self, value):
        if fingerprint(inventory(self.root)) != self.snapshot:
            raise Rejected("example_project_snapshot_changed")
        if value["capability"] == "help":
            description = ("可以检查示例工程的发布记录、变更和风险。请说：检查示例项目最近一次发布。"
                if self.declaration is None else "可用能力：\n" + "\n".join(
                    item["name"] + "：" + item["description"] for item in self.declaration["capabilities"]))
            return {"verified": True, "text": self.persona["name"] + description,
                    "evidence": self.snapshot}
        if self.declaration is not None and value["capability"] not in {item["name"] for item in self.declaration["capabilities"]}:
            raise Rejected("unregistered_project_capability")
        # Fixed entry point and arguments; sender/model text stays on stdin.
        env = {k: v for k, v in os.environ.items() if k in {"SYSTEMROOT", "SystemRoot", "WINDIR", "TEMP", "TMP"}}
        env["PATH"] = os.defpath
        entrypoint = "im_agent_project.py" if self.declaration is not None else "inspect_release.py"
        request = {"question": value["input"]["text"]}
        if self.declaration is not None:
            request.update(schema=1, capability=value["capability"])
        # Files avoid buffering untrusted child output in host memory. Discard
        # stderr; private program diagnostics must use its own protected sink.
        with tempfile.TemporaryFile() as output:
            result = subprocess.run([sys.executable, "-I", "-B", "-X", "utf8", str(self.root / entrypoint)],
                     input=json.dumps(request, ensure_ascii=False).encode("utf-8"),
                     cwd=self.root, env=env, stdout=output, stderr=subprocess.DEVNULL, timeout=15, **background_options())
            output.seek(0); raw = output.read(32769)
        if result.returncode or len(raw) > 32768:
            raise Rejected("example_project_execution_failed")
        try:
            report = json.loads(raw.decode("utf-8"))
            if set(report) != {"text"} or not isinstance(report["text"], str) or not 1 <= len(report["text"]) <= 7000:
                raise ValueError("result")
        except (ValueError, TypeError):
            raise Rejected("example_project_result_invalid") from None
        return {"verified": True, "text": report["text"], "evidence": self.snapshot}
