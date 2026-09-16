@echo off
REM Stop the MT5 monitor bot. Kills python.exe / pythonw.exe whose command line
REM runs mt5_bot.py — REGARDLESS of how it was started (run_mt5bot.bat, the
REM autostart task, a background shell). The pattern is STRICT: it never
REM matches the Claude bot's bot.py, and the Name filter keeps this sweeper
REM (powershell.exe carrying the pattern text in its own command line) safe.
setlocal enableextensions

powershell -NoProfile -ExecutionPolicy Bypass -Command "$p = Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | Where-Object { $_.CommandLine -match 'mt5_bot\.py' }; if (-not $p) { Write-Host 'MT5 bot is not running.' } else { $p | ForEach-Object { Write-Host ('Stopping PID ' + $_.ProcessId + ' (' + $_.Name + ')'); try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }; Write-Host 'MT5 bot stopped.' }"

echo.
pause
endlocal
