@echo off
REM Manual launcher for the MT5 terminal Telegram monitor bot.
REM The venv lives at the REPO ROOT (one folder up); the bot itself is mt5_bot.py
REM next to this script. Logs go to mt5bot\logs\mt5bot.log and the console.
chcp 65001 >nul
cd /d "%~dp0.."
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
) else (
    echo [run_mt5bot] .venv not found - using system python
)

echo Starting MT5 Telegram monitor...
echo Close this window or press Ctrl+C to stop.
echo.
python -u mt5bot\mt5_bot.py

echo.
echo MT5 bot stopped. Press any key to close.
pause >nul
