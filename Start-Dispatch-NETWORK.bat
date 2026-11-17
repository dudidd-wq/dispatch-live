@echo off
REM cd first, while delayed expansion is off (a "!" in the folder name would be mangled)
cd /d "%~dp0"
setlocal enabledelayedexpansion
title DISPATCH LIVE - Network Mode (phone access)
color 0D
echo.
echo  ==================================================
echo    DISPATCH LIVE  -  NETWORK MODE
echo    (lets your phone/tablet reach the deck over Wi-Fi)
echo  ==================================================
echo.

REM Enable network binding (0.0.0.0) so other devices on the LAN can connect.
set "NETWORK=1"

set "RUN="
py -3 -c "pass" >nul 2>nul && set "RUN=py -3 server.py"
if not defined RUN py -c "pass" >nul 2>nul && set "RUN=py server.py"
if not defined RUN python -c "pass" >nul 2>nul && set "RUN=python server.py"
if not defined RUN python3 -c "pass" >nul 2>nul && set "RUN=python3 server.py"
if not defined RUN where node >nul 2>nul && set "RUN=node server.js"
if not defined RUN goto noengine

echo  [OK] Engine: !RUN!
echo.

REM Close any old server on port 3100 first.
set "PORTPID="
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /c:":3100 " ^| findstr "LISTENING"') do set "PORTPID=%%a"
if defined PORTPID taskkill /PID !PORTPID! /F >nul 2>nul

echo  Starting in NETWORK mode...
echo  The server window will print a  http://192.168.x.x:3100  address.
echo  Open THAT address in your phone's browser (same Wi-Fi network).
echo.
echo  NOTE: the first time, Windows may pop a Firewall prompt.
echo        Tick "Private networks" and click "Allow access".
echo.

!RUN!

REM Clean exit (deck window closed) - close this console immediately.
if "!errorlevel!"=="0" exit

echo.
echo  Server stopped with an error (code !errorlevel!).
echo  Press any key to close.
pause >nul
exit /b

:noengine
echo  [!] Neither Python nor Node.js was found.
echo  Install Python (https://www.python.org/downloads/) or Node.js (https://nodejs.org).
echo.
pause >nul
exit /b
