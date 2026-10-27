@echo off
cd /d %~dp0..\..
.venv\Scripts\tg-trader --config config.yaml status
.venv\Scripts\tg-trader --config config.yaml bridge-ping dest_demo
pause
