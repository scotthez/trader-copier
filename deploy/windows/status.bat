@echo off
cd /d %~dp0
tg-signal-trader\.venv\Scripts\tg-trader --config config.yaml status
tg-signal-trader\.venv\Scripts\tg-trader --config config.yaml bridge-ping dest_demo
pause
