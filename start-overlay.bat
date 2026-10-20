@echo off
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
  echo [ERROR] venv not found. Run the setup from README.md first.
  pause
  exit /b 1
)

venv\Scripts\python.exe -c "import PyQt6.QtWebEngineWidgets" >nul 2>&1
if errorlevel 1 (
  echo Installing the overlay components ^(one time, about 150 MB^)...
  venv\Scripts\pip install -r requirements-overlay.txt
  if errorlevel 1 (
    pause
    exit /b 1
  )
)

if /i "%~1"=="--list-buttons" (
  venv\Scripts\python.exe overlay.py --list-buttons
  exit /b 0
)

echo Starting the radio overlay. Find it in the system tray ^(bottom right^).
start "" venv\Scripts\pythonw.exe overlay.py %*
