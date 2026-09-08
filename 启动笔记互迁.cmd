@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Please run scripts\setup.ps1 first.
  pause
  exit /b 1
)
if not exist "ui\dist\index.html" (
  echo Please run npm run build --prefix ui first.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m note_bridge.app
