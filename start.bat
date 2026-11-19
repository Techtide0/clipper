@echo off
rem Starts Clipper and opens it in your browser. Leave this window open while you use the app.
cd /d "%~dp0" || (echo Couldn't enter the Clipper folder. & pause & exit /b 1)

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

cd backend || (echo Couldn't enter the backend folder. & pause & exit /b 1)
echo Starting Clipper at http://localhost:8000  (close this window to stop it)
rem Open the browser only once the server answers (up to 60 seconds); if it never does, its error stays visible below.
start "" /b powershell -NoProfile -Command "for($i=0;$i -lt 60;$i++){try{Invoke-WebRequest -UseBasicParsing http://localhost:8000/api/summary -TimeoutSec 2 | Out-Null; Start-Process 'http://localhost:8000'; exit}catch{Start-Sleep 1}}"
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
pause
