@echo off
setlocal
cd /d "%~dp0.."
if not exist .venv\Scripts\python.exe (echo ERROR: Run scripts\setup_windows.bat first. & pause & exit /b 1)

echo [1/4] Python compile check...
.venv\Scripts\python.exe -m compileall -q backend || exit /b 1

echo [2/4] Backend tests...
set PYTHONPATH=backend
.venv\Scripts\python.exe -m pytest -q backend\tests || exit /b 1

echo [3/4] API import...
.venv\Scripts\python.exe -c "import sys; sys.path.insert(0, 'backend'); import main; print('API import OK')" || exit /b 1

echo [4/4] Frontend production build...
cd dashboard
call npm run build || exit /b 1
cd ..

echo.
echo Verification passed.
pause
