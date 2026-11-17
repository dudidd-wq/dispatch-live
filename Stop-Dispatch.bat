@echo off
REM cd first, while delayed expansion is off (a "!" in the folder name would be mangled)
cd /d "%~dp0"
setlocal enabledelayedexpansion
title Stop Dispatch LIVE
color 0C
echo.
echo  ==================================================
echo    STOP DISPATCH LIVE  -  closing all windows
echo  ==================================================
echo.

REM --- 1) Close Dispatch browser windows (our deck profile only) ---
REM     We launched Edge with --user-data-dir=%TEMP%\DispatchDeckProfile,
REM     so we target ONLY those processes, never your normal Edge.
echo  Closing Dispatch browser windows...
powershell -NoProfile -Command "$p=Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'DispatchDeckProfile' }; if($p){$p|ForEach-Object{Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue}}" >nul 2>nul

REM --- 2) Stop the local server on port 3100 ---
echo  Stopping the local server on port 3100...
set "PORTPID="
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /c:":3100 " ^| findstr "LISTENING"') do set "PORTPID=%%a"
if defined PORTPID taskkill /PID !PORTPID! /F >nul 2>nul

REM --- 3) Stop the Traffic Brain on port 3200 ---
echo  Stopping the Traffic Brain on port 3200...
set "BRAINPID="
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /c:":3200 " ^| findstr "LISTENING"') do set "BRAINPID=%%a"
if defined BRAINPID taskkill /PID !BRAINPID! /F >nul 2>nul
taskkill /FI "WINDOWTITLE eq Traffic Brain*" /F >nul 2>nul

REM --- 4) Close leftover launcher windows ---
taskkill /FI "WINDOWTITLE eq DISPATCH LIVE*" /F >nul 2>nul

echo.
echo  [OK] All Dispatch windows and the server have been closed.
echo  You can close this window now.
echo.
pause >nul
exit /b
