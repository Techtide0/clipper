@echo off
rem One-time setup: installs everything the app needs. Safe to run again.
cd /d "%~dp0"

where python >nul 2>nul || (echo Python was not found. Install Python 3.12 from https://www.python.org/downloads/ and run this again. & pause & exit /b 1)
where node >nul 2>nul   || (echo Node.js was not found. Install it from https://nodejs.org/ and run this again. & pause & exit /b 1)
where ffmpeg >nul 2>nul || echo NOTE: ffmpeg was not found. Install it with:  winget install Gyan.FFmpeg   (then open a new window)

if not exist backend\.venv\Scripts\python.exe (
  echo Creating the Python environment...
  python -m venv backend\.venv || (pause & exit /b 1)
)
echo Installing Python packages...
backend\.venv\Scripts\python.exe -m pip install --upgrade pip >nul
backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt || (pause & exit /b 1)

if not exist backend\.env (
  copy backend\.env.example backend\.env >nul
  echo Created backend\.env: open it and paste your GEMINI_API_KEY after the = sign.
)

echo Building the dashboard...
pushd frontend
call npm install || (popd & pause & exit /b 1)
call npm run build || (popd & pause & exit /b 1)
popd

echo.
echo Setup finished. Start the app by double-clicking start.bat
pause
