@echo off
REM Stop any running instance of the bot.
REM Kills python.exe / pythonw.exe whose command line runs bot.py, regardless of
REM how it was started: run_bot.bat, the autostart task (run_autostart.vbs), or a
REM background shell. Detection-by-command-line is needed because taskkill can only
REM filter by image name, and other python processes may exist on this PC.
REM
REM Additionally kills the Claude Agent SDK's bundled CLI
REM (claude_agent_sdk\_bundled\claude.exe) that the sdk runner spawned: a hard
REM python kill orphans it (the SDK only reaps it on a graceful disconnect).
REM FILTERED by command line — the owner's interactive claude.exe
REM (~/.local/bin) has the same image name and must NOT be touched.
REM The Name='claude.exe' check also protects THIS script: the powershell below
REM carries the pattern text in its own command line, so a cmdline-only filter
REM would match (and kill) the sweeper itself.
setlocal enableextensions

powershell -NoProfile -ExecutionPolicy Bypass -Command "$p = Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | Where-Object { $_.CommandLine -match 'bot\.py' }; if (-not $p) { Write-Host 'Bot is not running.' } else { $p | ForEach-Object { Write-Host ('Stopping PID ' + $_.ProcessId + ' (' + $_.Name + ')'); try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }; Write-Host 'Bot stopped.' }"

powershell -NoProfile -ExecutionPolicy Bypass -Command "$p = Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.Name -eq 'claude.exe' -and $_.CommandLine -match 'claude_agent_sdk._bundled' }; if (-not $p) { Write-Host 'No orphaned SDK claude.exe.' } else { $p | ForEach-Object { Write-Host ('Stopping bundled SDK CLI PID ' + $_.ProcessId + ' (' + $_.Name + ')'); try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }; Write-Host 'SDK CLI stopped.' }"

echo.
pause
endlocal
