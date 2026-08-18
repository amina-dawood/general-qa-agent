@echo off
setlocal
cd /d "%~dp0.."
if not exist .venv\Scripts\python.exe (echo ERROR: Run scripts\setup_windows.bat first. & pause & exit /b 1)
if not exist dashboard\dist\index.html (
  echo Dashboard build missing. Building once...
  cd dashboard
  call npm run build || (echo ERROR: Dashboard build failed. & pause & exit /b 1)
  cd ..
)
if not exist .env (copy .env.example .env >nul & echo Created .env. Add OPENAI_API_KEY before using AI features.)
start "General QA Agent" cmd /k "cd /d %CD% && .venv\Scripts\python.exe backend\run_server.py"
timeout /t 2 /nobreak >nul
start "" http://127.0.0.1:8000
endlocal
