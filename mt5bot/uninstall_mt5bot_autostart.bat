@echo off
REM Remove the MT5 monitor autostart task (created by install_mt5bot_autostart.bat).
REM Only deletes the scheduled task; a running bot process is NOT killed.
setlocal enableextensions

schtasks /Delete /TN "MT5TelegramBot" /F
if errorlevel 1 (
  echo [uninstall] Task not found or already removed.
  exit /b 1
)

echo.
echo [uninstall] Task "MT5TelegramBot" removed. MT5 autostart is disabled.
echo Note: a bot that is currently running is NOT stopped by this.
endlocal
