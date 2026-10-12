@echo off
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
  echo [ERROR] venv not found. Recreate with:
  echo   python -m venv venv
  echo   venv\Scripts\pip install -r requirements.txt
  pause
  exit /b 1
)

venv\Scripts\python.exe -c "import sys" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] venv is broken ^(base Python missing^). Recreate it:
  echo   rmdir /s /q venv
  echo   python -m venv venv
  echo   venv\Scripts\pip install -r requirements.txt
  pause
  exit /b 1
)

echo [1/3] Starting server...
start "Server" /D "%~dp0" cmd /k venv\Scripts\python.exe server.py

echo [2/3] Waiting for server...
set /a tries=0
:wait
set /a tries+=1
if %tries% gtr 30 (
  echo [ERROR] Server did not become ready on http://127.0.0.1:8000
  echo Check the "Server" window for errors.
  pause
  exit /b 1
)
timeout /t 1 /nobreak >nul
venv\Scripts\python.exe -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000', timeout=1)" >nul 2>&1
if errorlevel 1 goto wait

echo [3/3] Server ready, starting simulator...
start "Simulator" /D "%~dp0" cmd /k venv\Scripts\python.exe simulator.py

echo Done. Open http://127.0.0.1:8000 if the browser did not open.
timeout /t 2 /nobreak >nul
