@echo off
cd /d "%~dp0"
echo Building LUFS-Lab.exe - this takes a few minutes...
py -m pip install --upgrade -r requirements.txt pyinstaller
py -m PyInstaller --noconfirm --noconsole --onefile --name LUFS-Lab --icon icon.ico --collect-all soundfile --collect-all tkinterdnd2 --hidden-import lameenc lufs_lab.py
if errorlevel 1 (
  echo Build failed - see messages above.
  if not defined CI pause
  exit /b 1
)
echo.
echo Done. Your program is: %~dp0dist\LUFS-Lab.exe
echo You can copy that single file anywhere; it does not need Python.
if not defined CI pause
