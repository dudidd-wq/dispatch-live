@echo off
setlocal
REM   DISPATCH LIVE - stop starting automatically at sign-in
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "DispatchLIVE" /f >nul 2>nul
if errorlevel 1 goto none
echo.
echo  [OK] Automatic start removed. The deck itself was not touched.
echo.
pause
exit /b 0

:none
echo.
echo  Nothing to remove - automatic start was not installed.
echo.
pause
exit /b 0
