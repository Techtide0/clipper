@echo off
rem Starts Clipper and opens it in your browser. Leave this window open while you use the app.
cd /d "%~dp0"

if not exist backend\.venv\Scripts\python.exe (
  echo Not set up yet. Double-click setup.bat first.
  pause
  exit /b 1
)
if not exist frontend\dist\index.html (
  echo The dashboard hasn't been built yet. Double-click setup.bat first.
  pause
  exit /b 1
)

echo Starting Clipper at http://localhost:8000  (close this window to stop it)
start "" "http://localhost:8000"
cd backend
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
pause
