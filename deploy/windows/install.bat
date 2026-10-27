@echo off
rem One-time, from a clone of trader-copier. Needs Python 3.11+ (python.org, tick "Add python.exe to PATH").
rem Installs in editable mode, so a later "git pull" updates the program without reinstalling.
cd /d %~dp0..\..
if not exist C:\tg-mirror mkdir C:\tg-mirror
if not exist .venv (py -3 -m venv .venv || python -m venv .venv)
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\pip install -e .
if not exist config.yaml copy deploy\windows\mirror-config.yaml config.yaml
echo.
echo Installed. Edit config.yaml in %CD% (paths), then run deploy\windows\status.bat.
pause
