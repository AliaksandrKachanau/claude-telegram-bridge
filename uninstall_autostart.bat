@echo off
REM Remove the Claude Telegram bot autostart task (created by install_autostart.bat).
REM This only deletes the scheduled task; a currently running bot process is NOT killed.
setlocal enableextensions

schtasks /Delete /TN "ClaudeTelegramBot" /F
if errorlevel 1 (
  echo [uninstall] Task not found or already removed.
  exit /b 1
)

echo.
echo [uninstall] Task "ClaudeTelegramBot" removed. Autostart is disabled.
echo Note: a bot that is currently running is NOT stopped by this.
endlocal
