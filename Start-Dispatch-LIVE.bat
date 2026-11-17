@echo off
REM cd first, while delayed expansion is off (a "!" in the folder name would be mangled)
cd /d "%~dp0"
setlocal enabledelayedexpansion
title DISPATCH LIVE - Command Deck
color 0B
echo.
echo  ==================================================
echo    DISPATCH LIVE  -  TV + News + Red Alerts
echo  ==================================================
echo.

REM --- Find Python (3.9 or newer). ---
set "RUN="
py -3 -c "pass" >nul 2>nul && set "RUN=py -3 server.py"
if not defined RUN py -c "pass" >nul 2>nul && set "RUN=py server.py"
if not defined RUN python -c "pass" >nul 2>nul && set "RUN=python server.py"
if not defined RUN python3 -c "pass" >nul 2>nul && set "RUN=python3 server.py"
if not defined RUN goto noengine

echo  [OK] Engine: !RUN!
echo.

REM ============================================================
REM   PRE-LAUNCH CLEANUP - close anything already running so we
REM   never stack duplicate windows or servers.
REM ============================================================
echo  Checking for instances already running...

REM 1) Close any server still listening on port 3100.
set "PORTPID="
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /c:":3100 " ^| findstr "LISTENING"') do set "PORTPID=%%a"
if defined PORTPID echo    closing old server on port 3100 [PID !PORTPID!]
if defined PORTPID taskkill /PID !PORTPID! /F >nul 2>nul

REM 2) Close any Dispatch browser windows (our dedicated profile only).
powershell -NoProfile -Command "$p=Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'DispatchDeckProfile' }; if($p){$p|ForEach-Object{Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue}}" >nul 2>nul

echo  Done.
echo.

REM ============================================================
REM   TRAFFIC BRAIN - standalone traffic service (port 3200).
REM   It owns the learned model, the API key and the budget, so
REM   it is the only thing that can ever spend a Google call.
REM   Runs in its own minimised window and keeps learning.
REM ============================================================
set "BRAINPID="
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /c:":3200 " ^| findstr "LISTENING"') do set "BRAINPID=%%a"
if defined BRAINPID (
  echo  [OK] Traffic Brain already running [PID !BRAINPID!]
) else (
  if exist brain.py (
    echo  [OK] Starting Traffic Brain ^(minimised window^)...
    start "Traffic Brain" /min cmd /c "!RUN:server.py=brain.py!"
  ) else (
    echo  [ ] brain.py not found - traffic will be unavailable.
  )
)
if not exist google.key echo  [ ] google.key missing - brain runs but cannot measure.
echo.

echo  Starting the server...
echo  (The deck will open in Edge fullscreen automatically once ready.)
echo.

REM --- Run the server. It opens the browser itself once it's listening. ---
REM     Closing this window stops the app.
!RUN!
REM keep the server's exit code: the commands below overwrite errorlevel
set "SRVEXIT=!errorlevel!"

REM The deck has stopped - take the Traffic Brain down with it.
echo.
echo  Closing the Traffic Brain...
set "BRAINPID="
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /c:":3200 " ^| findstr "LISTENING"') do set "BRAINPID=%%a"
if defined BRAINPID taskkill /PID !BRAINPID! /F >nul 2>nul
taskkill /FI "WINDOWTITLE eq Traffic Brain*" /F >nul 2>nul

REM Clean exit (deck window closed) - close this console immediately.
if "!SRVEXIT!"=="0" exit

echo.
echo  Server stopped with an error (code !SRVEXIT!).
echo  Press any key to close.
pause >nul
exit /b

:noengine
echo  [!] Python was not found.
echo.
echo  Install Python (free):  https://www.python.org/downloads/
echo  During setup tick "Add python.exe to PATH".
echo.
echo  Then run this file again.
echo.
pause >nul
exit /b
