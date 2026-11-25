"""开发工具:对运行中的服务测试"自由行动"意图解析(需要先 `python run.py`)。

用法:
    python -m tools.try_act "我不理陈姐,直接接电话" "我想飞到月亮上去"
默认在序幕之后的 studio 场景测试;--at 指定要先走的选择序列,如 --at start,cut_song。
"""
from __future__ import annotations

import argparse
import sys
import time

import httpx


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("texts", nargs="+")
    parser.add_argument("--at", default="start", help="先依次执行的选择 id,逗号分隔")
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    with httpx.Client(base_url=args.base, timeout=60) as client:
        for text in args.texts:
            sid = client.post("/api/session").json()["session_id"]
            scene = None
            for choice in filter(None, args.at.split(",")):
                scene = client.post("/api/choose", json={"session_id": sid, "choice_id": choice}).json()["scene"]
            started = time.monotonic()
            resp = client.post("/api/act", json={"session_id": sid, "text": text})
            data = resp.json()
            where = scene["title"] if scene else "序幕"
            print(f"[{where}] 玩家说: {text!r}")
            print(f"  HTTP {resp.status_code}  {time.monotonic() - started:.1f}s  -> {data}\n")
            time.sleep(1.1)  # 避开服务端的自由行动限流
    return 0


if __name__ == "__main__":
    sys.exit(main())
