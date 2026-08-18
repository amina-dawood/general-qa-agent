@echo off
setlocal
cd /d "%~dp0.."

echo ========================================================
echo General QA Agent - Windows Setup
echo ========================================================

where py >nul 2>nul || (echo ERROR: Python launcher not found. Install Python 3.12. & pause & exit /b 1)
if not exist .venv (
  echo Creating Python 3.12 virtual environment...
  py -3.12 -m venv .venv || (echo ERROR: Could not create .venv. & pause & exit /b 1)
)

call .venv\Scripts\activate.bat
python -m pip install --disable-pip-version-check -r backend\requirements-dev.txt || (echo ERROR: Python dependency installation failed. & pause & exit /b 1)

where npm >nul 2>nul || (echo ERROR: Node.js/npm not found. Install Node.js LTS. & pause & exit /b 1)
cd dashboard
call npm ci --no-audit --no-fund || (echo ERROR: npm dependency installation failed. & pause & exit /b 1)
call npm run build || (echo ERROR: Dashboard build failed. & pause & exit /b 1)
cd ..

if not exist .env copy .env.example .env >nul
if not exist data mkdir data

echo.
echo Setup complete.
echo 1. Open .env and set OPENAI_API_KEY.
echo 2. Run scripts\start_local.bat.
echo.
pause
