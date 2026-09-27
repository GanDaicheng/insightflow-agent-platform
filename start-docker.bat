@echo off
setlocal
cd /d "%~dp0"

title InsightFlow - Start Project
echo ================================================
echo   InsightFlow data intelligent Agent platform
echo   One-click startup
echo ================================================
echo.

where docker >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Docker was not found.
  echo Please install Docker Desktop first.
  pause
  exit /b 1
)

docker info >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Docker Desktop is not running.
  echo Please start Docker Desktop and run this shortcut again.
  pause
  exit /b 1
)

if not exist ".env" (
  echo [ERROR] .env was not found.
  echo Please copy .env.example to .env and configure APP_MODE first.
  pause
  exit /b 1
)

echo Starting the project with scripts\bootstrap.ps1...
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\bootstrap.ps1"
if errorlevel 1 (
  echo.
  echo [ERROR] Project startup failed.
  docker compose ps
  pause
  exit /b 1
)

echo.
echo [OK] Project is ready at http://localhost:3000
start "" http://localhost:3000
docker compose ps
pause
exit /b 0
