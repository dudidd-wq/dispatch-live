@echo off
REM ============================================================
REM   DISPATCH LIVE - background mode
REM   Runs the deck server and the Traffic Brain with NO console
REM   windows. Output goes to the logs\ folder instead.
REM   Stop it with Stop-Dispatch.bat.
REM ============================================================
REM cd first, while delayed expansion is still off: a folder name
REM containing "!" would otherwise be mangled.
cd /d "%~dp0"
setlocal enabledelayedexpansion

REM --- pythonw = Python without a console window ---
set "PYW="
pyw -3 -c "pass" >nul 2>nul && set "PYW=pyw -3"
if not defined PYW pythonw -c "pass" >nul 2>nul && set "PYW=pythonw"
if not defined PYW goto nopythonw

REM --- do not stack a second copy on top of a running one ---
set "PORTPID="
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /c:":3100 " ^| findstr "LISTENING"') do set "PORTPID=%%a"
if defined PORTPID goto already

REM --- keep running when the deck window is closed, and log to files ---
set "NOAUTOCLOSE=1"
set "DL_LOG=1"

set "BRAINPID="
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /c:":3200 " ^| findstr "LISTENING"') do set "BRAINPID=%%a"
if not defined BRAINPID if exist brain.py start "" !PYW! brain.py
start "" !PYW! server.py
exit /b 0

:already
REM Server is already up - just open the deck.
start "" http://localhost:3100
exit /b 0

:nopythonw
echo.
echo  [!] pythonw was not found.
echo      It is installed together with Python from python.org.
echo      Use Start-Dispatch-LIVE.bat instead, or reinstall Python.
echo.
pause
exit /b 1
