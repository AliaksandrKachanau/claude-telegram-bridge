@echo off
REM Manual launcher for the Claude Telegram bridge.
REM Double-click to start; logs go to logs\bot.log and the console.
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
) else (
    echo [run_bot] .venv not found - using system python
)

echo Starting Claude Telegram bridge...
echo Close this window or press Ctrl+C to stop.
echo.
python -u bot.py

echo.
echo Bot stopped. Press any key to close.
pause >nul
