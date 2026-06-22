@echo off
REM Register the Claude Telegram bot to start at user logon (Task Scheduler).
REM The task runs run_autostart.vbs, which launches the bot PAUSED (BOT_START_PAUSED=1).
REM No admin rights needed (runs as the current user with /RL LIMITED).
setlocal enableextensions

set "PROJ=%~dp0"
if "%PROJ:~-1%"=="\" set "PROJ=%PROJ:~0,-1%"
set "VBS=%PROJ%\run_autostart.vbs"

if not exist "%VBS%" (
  echo [install] run_autostart.vbs not found next to this script:
  echo   %VBS%
  exit /b 1
)

schtasks /Create /SC ONLOGON /TN "ClaudeTelegramBot" /RL LIMITED /F /TR "wscript.exe \"%VBS%\""
if errorlevel 1 (
  echo [install] Failed to create the scheduled task.
  exit /b 1
)

echo.
echo [install] Task "ClaudeTelegramBot" created.
echo The bot will start at your next logon, PAUSED by default (/resume to enable Claude).
echo It is NOT started right now (to avoid a duplicate beside a running bot).
echo Run it now:    schtasks /Run /TN "ClaudeTelegramBot"
echo Remove it:     uninstall_autostart.bat
endlocal
