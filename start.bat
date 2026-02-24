@echo off
REM ZeroClaw + OpenClaw startup (Windows)
REM Double-click or run from cmd to start services.
cd /d "%~dp0"
python start.py %*
if errorlevel 1 pause
