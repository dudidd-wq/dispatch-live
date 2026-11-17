@echo off
title Check Dispatch LIVE - what's running
color 0E
echo.
echo  ==================================================
echo    CHECK DISPATCH LIVE  -  background instances
echo  ==================================================
echo.

echo  [1] Server(s) listening on port 3100:
echo  --------------------------------------------------
netstat -ano | findstr /c:":3100 " | findstr "LISTENING"
if errorlevel 1 echo     (none - the server is not running)
echo.

echo  [1b] Traffic Brain on port 3200:
echo  --------------------------------------------------
netstat -ano | findstr /c:":3200 " | findstr "LISTENING"
if errorlevel 1 echo     (none - the traffic brain is not running)
echo.

echo  [2] Dispatch browser app windows (our DispatchDeck profile):
echo  --------------------------------------------------
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'DispatchDeckProfile' }; if ($p) { $p | ForEach-Object { 'PID ' + $_.ProcessId + '  ' + $_.Name } } else { '    (none - no Dispatch app windows open)' }"
echo.

echo  [3] All python/node processes (may include other apps):
echo  --------------------------------------------------
tasklist /FI "IMAGENAME eq python.exe" /FO TABLE /NH 2>nul | findstr /R "." || echo     (no python.exe)
tasklist /FI "IMAGENAME eq py.exe" /FO TABLE /NH 2>nul | findstr /R "." || echo     (no py.exe)
tasklist /FI "IMAGENAME eq node.exe" /FO TABLE /NH 2>nul | findstr /R "." || echo     (no node.exe)
echo.

echo  --------------------------------------------------
echo  To close everything cleanly, run:  Stop-Dispatch.bat
echo.
pause >nul
exit /b
