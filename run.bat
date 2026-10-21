@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^), then run this again.
  pause
  exit /b 1
)
echo Checking required packages ^(first run takes a minute^)...
py -m pip install --quiet -r requirements.txt
if errorlevel 1 (
  echo Package install failed. Check your internet connection.
  pause
  exit /b 1
)
start "" pyw "%~dp0lufs_lab.py"
