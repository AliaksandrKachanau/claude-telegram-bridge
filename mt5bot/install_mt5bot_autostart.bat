@echo off
REM Register the MT5 monitor bot to start at user logon (Task Scheduler).
REM Runs run_mt5autostart.vbs from THIS folder. No admin rights needed
REM (/RL LIMITED, current user) — same pattern as the Claude bot's autostart.
setlocal enableextensions

set "DIR=%~dp0"
if "%DIR:~-1%"=="\" set "DIR=%DIR:~0,-1%"
set "VBS=%DIR%\run_mt5autostart.vbs"

if not exist "%VBS%" (
  echo [install] run_mt5autostart.vbs not found next to this script:
  echo   %VBS%
  exit /b 1
)

schtasks /Create /SC ONLOGON /TN "MT5TelegramBot" /RL LIMITED /F /TR "wscript.exe \"%VBS%\""
if errorlevel 1 (
  echo [install] Failed to create the scheduled task.
  exit /b 1
)

echo.
echo [install] Task "MT5TelegramBot" created.
echo The MT5 monitor will start at your next logon (ACTIVE - it only notifies).
echo It is NOT started right now (to avoid a duplicate beside a running bot).
echo Run it now:    schtasks /Run /TN "MT5TelegramBot"
echo Remove it:     uninstall_mt5bot_autostart.bat
endlocal
