@echo off
rem One click: installs whatever is missing, starts everything and opens the app in the browser. Close this window to stop the app.
title Deminic
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_services.ps1" -Start
if errorlevel 1 pause
