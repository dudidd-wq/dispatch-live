@echo off
setlocal
REM ============================================================
REM   DISPATCH LIVE - start automatically when you sign in
REM   Registers a Windows scheduled task for THIS user that runs
REM   Start-Dispatch-BACKGROUND.bat at logon. No admin rights
REM   are needed. Remove it with Uninstall-Autostart.bat.
REM ============================================================
cd /d "%~dp0"
if not exist "%~dp0Start-Dispatch-BACKGROUND.bat" goto missing

schtasks /create /f /tn "Dispatch LIVE" /sc onlogon /rl limited /tr "\"%~dp0Start-Dispatch-BACKGROUND.bat\""
if errorlevel 1 goto failed

echo.
echo  [OK] Dispatch LIVE will start by itself at every sign-in.
echo       It runs in the background - there is no console window.
echo       Logs:   %~dp0logs
echo       Stop:   Stop-Dispatch.bat
echo       Remove: Uninstall-Autostart.bat
echo.
pause
exit /b 0

:missing
echo  [!] Start-Dispatch-BACKGROUND.bat is not in this folder.
pause
exit /b 1

:failed
echo.
echo  [!] Windows refused to create the scheduled task.
echo      Nothing was changed. You can still start the deck by hand.
echo.
pause
exit /b 1
