@echo off
REM One-click setup for the Claude Telegram bridge (run once on a fresh machine).
REM Creates .venv, installs requirements.txt, copies .env.example -> .env if needed,
REM and checks the Python version + ffmpeg. If .venv already exists, it asks whether
REM to reinstall/refresh the dependencies or uninstall (.venv). Idempotent + safe:
REM uninstall removes only .venv (source, .env, config.yaml, dictations/ stay intact).
REM After this: fill .env / config.yaml, then double-click run_bot.bat.
setlocal enableextensions
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

echo ==================================================
echo   Claude Telegram bridge - setup
echo ==================================================
echo.

REM ---- Decide what to do when dependencies are already installed ----
if exist ".venv\Scripts\python.exe" (
  echo Dependencies are already installed ^(.venv exists^).
  echo.
  choice /c RUC /n /m "[R]einstall / refresh, [U]ninstall (.venv), [C]ancel? "
  if errorlevel 3 goto :cancel
  if errorlevel 2 goto :uninstall
  REM [R] -> fall through to the install path below.
)
goto :install

:uninstall
echo.
echo [..] Removing .venv ^(dependencies^) ...
rmdir /s /q ".venv"
if exist ".venv\Scripts\python.exe" (
  echo [FAIL] Could not fully remove .venv ^(files in use? stop the bot first^).
) else (
  echo [OK] .venv removed - dependencies uninstalled.
  echo      Source, .env, config.yaml and dictations/ were left intact.
  echo      Re-run setup.bat to install again.
)
echo.
pause
exit /b 0

:cancel
echo.
echo Cancelled.
pause
exit /b 0

:install

REM ---- 1. Locate Python 3.10+ (try 'python', then 'py -3') ----
set "PYEXE="
python --version >nul 2>nul
if %errorlevel%==0 set "PYEXE=python"

if defined PYEXE goto :pyfound
py -3 --version >nul 2>nul
if %errorlevel%==0 set "PYEXE=py -3"

:pyfound
if not defined PYEXE (
  echo [FAIL] Python not found on PATH.
  echo        Install Python 3.10+ from https://www.python.org/ and re-run setup.
  echo.
  pause
  exit /b 1
)

REM Parse "Python 3.12.x" -> major / minor.
set "PYVER="
for /f "tokens=2 delims= " %%v in ('%PYEXE% --version 2^>^&1') do set "PYVER=%%v"
set "PYMAJOR=0"
set "PYMINOR=0"
for /f "tokens=1,2 delims=." %%a in ("%PYVER%") do (
  set "PYMAJOR=%%a"
  set "PYMINOR=%%b"
)

if not "%PYMAJOR%"=="3" goto :badpy
if %PYMINOR% LSS 10 goto :badpy
echo [OK] Python %PYVER%  ^(%PYEXE%^)
goto :pyok

:badpy
echo [FAIL] Python %PYVER% found, but 3.10+ is required.
echo        Install Python 3.10+ and re-run setup.
echo.
pause
exit /b 1

:pyok
echo.

REM ---- 2. Create .venv if missing (clear a broken/partial one first) ----
if exist ".venv\Scripts\python.exe" (
  echo [OK] .venv already exists - refreshing dependencies.
) else (
  if exist ".venv" rmdir /s /q ".venv"
  echo [..] Creating .venv ...
  %PYEXE% -m venv .venv
  if not exist ".venv\Scripts\python.exe" (
    echo [FAIL] Could not create .venv.
    echo.
    pause
    exit /b 1
  )
  echo [OK] .venv created.
)
echo.

REM ---- 3. Install requirements ----
if not exist "requirements.txt" (
  echo [FAIL] requirements.txt not found - run setup.bat from the project folder.
  echo.
  pause
  exit /b 1
)
echo [..] Installing dependencies ^(can take a minute^) ...
.venv\Scripts\python -m pip install --upgrade pip >nul 2>nul
.venv\Scripts\python -m pip install -r requirements.txt
if errorlevel 1 (
  echo [FAIL] pip install failed - see messages above.
  echo.
  pause
  exit /b 1
)
.venv\Scripts\python -c "import telegram, yaml, dotenv, edge_tts" >nul 2>nul
if errorlevel 1 (
  echo [!]  Packages installed but an import check failed - review pip output.
) else (
  echo [OK] Dependencies installed and verified.
)
echo.

REM ---- 4. ffmpeg (needed for TTS / voice OGG encoding) ----
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo [!]  ffmpeg NOT on PATH - voice replies ^(/speak, voice input^) won't work.
  echo      Install:  winget install Gyan.FFmpeg
  echo      ^(or point tts.ffmpeg_path in config.yaml^)
) else (
  echo [OK] ffmpeg found on PATH.
)
echo.

REM ---- 5. .env (secrets - never in repo) ----
if exist ".env" goto :haveenv
if exist ".env.example" (
  copy /y ".env.example" ".env" >nul
  echo [OK] Copied .env.example -^> .env ^(now fill in your real values^).
  echo      TELEGRAM_BOT_TOKEN, ALLOWED_USER_IDS ^(+ GROQ_API_KEY for voice^).
) else (
  echo [!]  .env NOT found and no .env.example to copy - the bot will not start.
  echo      Create .env with: TELEGRAM_BOT_TOKEN, ALLOWED_USER_IDS, GROQ_API_KEY.
)
goto :envdone

:haveenv
echo [OK] .env exists.
:envdone
echo.

echo ==================================================
echo   Setup done.
echo   Next: fill .env ^(if not yet^) + config.yaml,
echo   then double-click run_bot.bat
echo ==================================================
echo.
pause
endlocal
