@echo off
rem Keeps the mirror running; restarts it 5 s after any exit. Close this window to stop it.
cd /d %~dp0..\..
:loop
.venv\Scripts\tg-trader --config config.yaml run %*
echo tg-trader exited, restarting in 5 s (close the window to stop)
timeout /t 5 /nobreak >nul
goto loop
