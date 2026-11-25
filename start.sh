#!/usr/bin/env sh
# macOS / Linux 一键启动:自动创建虚拟环境、安装依赖并运行游戏
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "[setup] creating virtual environment..."
  python3 -m venv .venv
fi
.venv/bin/python -m pip install -q -r requirements.txt
exec .venv/bin/python run.py
