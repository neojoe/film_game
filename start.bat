@echo off
rem Windows 一键启动:自动创建虚拟环境、安装依赖并运行游戏
cd /d "%~dp0"
setlocal
where python >nul 2>nul && (set "PY=python") || (set "PY=py -3")
if not exist ".venv\Scripts\python.exe" (
  echo [setup] creating virtual environment...
  %PY% -m venv .venv || (echo 需要 Python 3.10+ 并加入 PATH & exit /b 1)
)
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || exit /b 1
".venv\Scripts\python.exe" run.py
