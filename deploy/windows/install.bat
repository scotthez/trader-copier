@echo off
rem One-time: Python 3.11+ must be installed (python.org, tick "Add python.exe to PATH").
cd /d %~dp0
if not exist C:\tg-mirror mkdir C:\tg-mirror
py -3 -m venv tg-signal-trader\.venv || python -m venv tg-signal-trader\.venv
tg-signal-trader\.venv\Scripts\python -m pip install --upgrade pip
tg-signal-trader\.venv\Scripts\pip install .\tg-signal-trader
if not exist config.yaml copy mirror-config.yaml config.yaml
echo.
echo Installed. Edit config.yaml (paths), then run status.bat.
pause
