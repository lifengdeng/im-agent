"""Operator-reviewed JSON bridge. Python stdlib only; no network or private tools."""
import json
from pathlib import Path
import sys


def main():
    request = json.loads(sys.stdin.read(8193))
    if (not isinstance(request, dict) or set(request) != {"schema", "capability", "question"}
            or type(request["schema"]) is not int or request["schema"] != 1
            or not isinstance(request["question"], str) or not 1 <= len(request["question"]) <= 1000):
        raise ValueError("invalid_request")
    project = json.loads((Path(__file__).parent / "project.json").read_text(encoding="utf-8"))
    if request["capability"] == "inspect-status":
        passed = sum(item["status"] == "passed" for item in project["checks"])
        pending = [item["name"] for item in project["checks"] if item["status"] != "passed"]
        text = (f"工程 {project['name']}，版本 {project['version']}。\n"
                f"检查通过 {passed}/{len(project['checks'])}；需复核：{'、'.join(pending) or '无'}。")
    elif request["capability"] == "list-checks":
        text = "工程检查：\n" + "\n".join(item["name"] + "：" + item["status"] for item in project["checks"])
    else:
        raise ValueError("unsupported_capability")
    print(json.dumps({"text": text + "\n依据：固定工程快照中的 project.json；本示例未现场运行这些检查。"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
