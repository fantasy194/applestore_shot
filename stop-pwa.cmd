@echo off
cd /d "%~dp0"
call pnpm.cmd pwa:stop
if errorlevel 1 pause
