@echo off
REM Stop the MT5 monitor bot. Kills python.exe / pythonw.exe whose command line
REM runs mt5_bot.py — REGARDLESS of how it was started (run_mt5bot.bat, the
REM autostart task, a background shell). The pattern is STRICT: it never
REM matches the Claude bot's bot.py, and the Name filter keeps this sweeper
REM (powershell.exe carrying the pattern text in its own command line) safe.
setlocal enableextensions

powershell -NoProfile -ExecutionPolicy Bypass -Command "$p = Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | Where-Object { $_.CommandLine -match 'mt5_bot\.py' }; if (-not $p) { Write-Host 'MT5 bot is not running.' } else { $p | ForEach-Object { Write-Host ('Stopping PID ' + $_.ProcessId + ' (' + $_.Name + ')'); try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }; Write-Host 'MT5 bot stopped.' }"

REM A bot started from an ELEVATED (admin) console hides its command line
REM from a normal query — the matcher above sees nothing while the bot runs
REM (and cannot kill it either: access denied). Warn instead of staying quiet.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$hidden = @(Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | Where-Object { -not $_.CommandLine }); if ($hidden.Count -gt 0) { Write-Host ('WARNING: ' + $hidden.Count + ' python process(es) with hidden command line (started as ADMIN?). This script cannot match or stop them - close the bot window yourself or reboot, then start run_mt5bot.bat WITHOUT admin rights.') }"

echo.
pause
endlocal
