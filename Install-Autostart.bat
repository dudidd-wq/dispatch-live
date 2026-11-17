@echo off
REM ============================================================
REM   DISPATCH LIVE - start automatically when you sign in
REM   Adds one entry for THIS Windows user under
REM     HKCU\Software\Microsoft\Windows\CurrentVersion\Run
REM   which runs Start-Dispatch-BACKGROUND.bat at sign-in.
REM   No admin rights needed. Remove with Uninstall-Autostart.bat.
REM ============================================================
cd /d "%~dp0"
REM The path is put in a variable first and used through delayed expansion:
REM a folder name with "&" or "(" would otherwise break the reg command.
set "TARGET=%~dp0Start-Dispatch-BACKGROUND.bat"
set "LOGDIR=%~dp0logs"
setlocal enabledelayedexpansion
if not exist "!TARGET!" goto missing

reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "DispatchLIVE" /t REG_SZ /d "\"!TARGET!\"" /f >nul
if errorlevel 1 goto failed

echo.
echo  [OK] Dispatch LIVE will start by itself at every sign-in.
echo       It runs in the background - there is no console window.
echo       Logs:   !LOGDIR!
echo       Stop:   Stop-Dispatch.bat
echo       Remove: Uninstall-Autostart.bat
echo.
echo  If you move this folder, run this file again from the new place.
echo.
pause
exit /b 0

:missing
echo  [!] Start-Dispatch-BACKGROUND.bat is not in this folder.
pause
exit /b 1

:failed
echo.
echo  [!] Windows refused to add the startup entry.
echo      Nothing was changed. You can still start the deck by hand.
echo.
pause
exit /b 1
