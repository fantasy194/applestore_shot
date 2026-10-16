@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0local-service.ps1" -Stop
if errorlevel 1 pause
