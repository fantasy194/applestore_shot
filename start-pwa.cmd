@echo off
cd /d "%~dp0"
call pnpm.cmd pwa
if errorlevel 1 pause
