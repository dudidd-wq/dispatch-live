@echo off
REM Drag your Word copy of Hayom Yom onto this file.
REM It writes hayomyom.json here, for the Shabbat band. Nothing is uploaded.
cd /d "%~dp0"
setlocal enabledelayedexpansion
if "%~1"=="" (
  echo.
  echo  Drag the .docx file onto Make-HayomYom.bat
  echo.
  pause
  exit /b 1
)
set "PY="
py -3 -c "pass" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "pass" >nul 2>nul && set "PY=python"
if not defined PY (
  echo  [!] Python was not found.
  pause
  exit /b 1
)
!PY! make_hayomyom.py "%~1"
echo.
pause
