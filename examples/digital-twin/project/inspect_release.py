"""Synthetic local engineering capability, deliberately independent of any AI SDK."""
import json
from pathlib import Path
import sys


def main():
    request = json.loads(sys.stdin.read(8193))
    if set(request) != {"question"} or not isinstance(request["question"], str):
        raise ValueError("question_required")
    releases = json.loads((Path(__file__).parent / "releases.json").read_text(encoding="utf-8"))
    previous, current = releases
    before, after = previous["traffic_percent"], current["traffic_percent"]
    risk = "需要复核：流量扩大超过 30 个百分点，建议先确认灰度观察结果。" if after - before > 30 else "流量增幅未触发示例规则。"
    text = (f"【合成工程】最近发布 {current['version']}，前一版 {previous['version']}。\n"
            f"变更：{current['change']}；流量 {before}% → {after}%。\n"
            f"审阅：{risk}\n依据：固定快照内 releases.json 的两条记录；仅覆盖示例规则。")
    print(json.dumps({"text": text}, ensure_ascii=False))


if __name__ == "__main__":
    main()
