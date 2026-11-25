@echo off
rem POE2 Price Checker - double-click to start.
rem First run: creates a private Python environment (.venv) and installs packages.
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
title POE2 Price Checker

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (
  echo.
  echo  [!] Python not found.
  echo      Install Python 3.10+ from https://www.python.org/downloads/
  echo      and tick "Add python.exe to PATH" during setup, then run this file again.
  echo.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo  First run: setting up... ^(about 1 minute^)
  %PY% -m venv .venv || goto :fail
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || goto :fail
)

echo  Starting POE2 Price Checker - the page opens in your browser.
echo  Close this window to stop the program.
".venv\Scripts\python.exe" web.py --open
exit /b 0

:fail
echo.
echo  [!] Setup failed. Delete the .venv folder and try again.
pause
exit /b 1
