@echo off
cd /d %~dp0..\..
.venv\Scripts\tg-trader --config config.yaml mirror-latency --n 50
pause
