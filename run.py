"""一条命令启动游戏:  python run.py

默认监听 http://127.0.0.1:8000 并自动打开浏览器。
可用环境变量:HOST / PORT / NO_BROWSER=1
"""
from __future__ import annotations

import logging
import os
import threading
import webbrowser

import uvicorn


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    url = f"http://{host}:{port}"
    if not os.getenv("NO_BROWSER"):
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"\n  听雨 FM · 最后一夜  →  {url}\n  按 Ctrl+C 退出\n")
    uvicorn.run("app.main:create_app", host=host, port=port, factory=True, log_level="warning")


if __name__ == "__main__":
    main()
