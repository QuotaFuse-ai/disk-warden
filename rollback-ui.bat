@echo off
title Disk Warden - UI prototype rollback
cd /d "%~dp0"
where node >/dev/null 2>/dev/null
if errorlevel 1 (
  echo [ERR] Node.js not found in PATH. Install Node.js, then double-click again.
  pause
  exit /b 1
)
node tools\rollback-ui.mjs %*
echo.
pause
