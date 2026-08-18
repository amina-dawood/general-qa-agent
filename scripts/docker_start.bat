@echo off
setlocal
cd /d "%~dp0.."
where docker >nul 2>nul || (echo ERROR: Docker Desktop / Docker CLI not found. & pause & exit /b 1)
if not exist .env copy .env.example .env >nul
if not exist .env (echo ERROR: .env could not be created. & pause & exit /b 1)
docker compose up -d --build || (echo ERROR: Docker deployment failed. & pause & exit /b 1)
echo.
echo General QA Agent is starting at http://127.0.0.1:8000
start "" http://127.0.0.1:8000
endlocal
