@echo off
REM Stop any running instance of the bot.
REM Kills python.exe / pythonw.exe whose command line runs bot.py, regardless of
REM how it was started: run_bot.bat, the autostart task (run_autostart.vbs), or a
REM background shell. Detection-by-command-line is needed because taskkill can only
REM filter by image name, and other python processes may exist on this PC.
setlocal enableextensions

powershell -NoProfile -ExecutionPolicy Bypass -Command "$p = Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | Where-Object { $_.CommandLine -match 'bot\.py' }; if (-not $p) { Write-Host 'Bot is not running.' } else { $p | ForEach-Object { Write-Host ('Stopping PID ' + $_.ProcessId + ' (' + $_.Name + ')'); try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }; Write-Host 'Bot stopped.' }"

echo.
pause
endlocal
