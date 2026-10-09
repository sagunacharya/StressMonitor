@echo off
setlocal EnableExtensions
title Stress Monitor

cd /d "%~dp0"

set "ROOT=%~dp0"
set "BACKEND=%ROOT%backend"
set "VENV=%BACKEND%\.venv"
set "PYEXE=%VENV%\Scripts\python.exe"
set "URL=http://127.0.0.1:5000"
set "HEALTH=http://127.0.0.1:5000/health"

echo ============================================
echo       STRESS MONITOR STARTUP
echo ============================================
echo.

REM ------------------------------------------------
REM Check backend files
REM ------------------------------------------------

if not exist "%BACKEND%\app.py" (
    echo [ERROR] backend\app.py was not found.
    echo.
    pause
    exit /b 1
)

if not exist "%BACKEND%\.env" (
    echo [ERROR] backend\.env is missing.
    echo.
    echo Create this file:
    echo %BACKEND%\.env
    echo.
    pause
    exit /b 1
)

REM ------------------------------------------------
REM Check if backend is already running
REM ------------------------------------------------

curl -s -o nul -w "%%{http_code}" "%HEALTH%" > "%TEMP%\stress_health.txt" 2>nul

set /p HTTP_CODE=<"%TEMP%\stress_health.txt"

if "%HTTP_CODE%"=="200" (
    echo [OK] Backend is already running.
    echo [OK] Health check returned HTTP 200.
    echo.
    echo Opening dashboard...
    start "" "%URL%"
    del "%TEMP%\stress_health.txt" >nul 2>nul
    echo.
    echo Dashboard: %URL%
    echo.
    pause
    exit /b 0
)

del "%TEMP%\stress_health.txt" >nul 2>nul

REM ------------------------------------------------
REM Check port 5000
REM ------------------------------------------------

netstat -ano | findstr ":5000" | findstr "LISTENING" >nul 2>nul

if not errorlevel 1 (
    echo [ERROR] Port 5000 is already in use.
    echo.
    echo Run:
    echo   netstat -ano ^| findstr :5000
    echo.
    echo If the Stress Monitor backend is already running,
    echo simply open:
    echo   %URL%
    echo.
    pause
    exit /b 1
)

REM ------------------------------------------------
REM Find Python
REM ------------------------------------------------

where py >nul 2>nul

if not errorlevel 1 (
    set "PYLAUNCHER=py -3.13"
) else (
    where python >nul 2>nul

    if not errorlevel 1 (
        set "PYLAUNCHER=python"
    ) else (
        echo [ERROR] Python was not found.
        echo.
        pause
        exit /b 1
    )
)

REM ------------------------------------------------
REM Create virtual environment if necessary
REM ------------------------------------------------

if not exist "%PYEXE%" (
    echo [INFO] Creating Python virtual environment...
    echo.

    %PYLAUNCHER% -m venv "%VENV%"

    if not exist "%PYEXE%" (
        echo.
        echo [ERROR] Could not create Python virtual environment.
        echo.
        pause
        exit /b 1
    )

    echo [OK] Virtual environment created.
)

REM ------------------------------------------------
REM Install/update pinned requirements when manifest hash changes
REM ------------------------------------------------

if exist "%BACKEND%\requirements.txt" (
    echo.
    echo [INFO] Checking pinned Python requirements...
    "%PYEXE%" "%BACKEND%\install_requirements.py"
    if errorlevel 1 (
        echo.
        echo [ERROR] Python requirements installation failed.
        echo Use: "%PYEXE%" "%BACKEND%\install_requirements.py" --force
        echo.
        pause
        exit /b 1
    )
)

REM ------------------------------------------------
REM Start backend
REM ------------------------------------------------

echo.
echo [INFO] Starting backend...
echo.

if exist "%ROOT%.stress_monitor_backend.log" del "%ROOT%.stress_monitor_backend.log"
if exist "%ROOT%.stress_monitor_backend_error.log" del "%ROOT%.stress_monitor_backend_error.log"

powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%Start Stress Monitor Backend.ps1"
if errorlevel 1 (
    echo.
    echo [ERROR] Could not launch the backend. Check Python and launcher paths.
    pause
    exit /b 1
)

REM ------------------------------------------------
REM Wait for backend
REM ------------------------------------------------

echo [INFO] Waiting for backend...

set /a COUNT=0

:WAIT

set /a COUNT+=1

curl -s -o nul -w "%%{http_code}" "%HEALTH%" > "%TEMP%\stress_health.txt" 2>nul

set /p HTTP_CODE=<"%TEMP%\stress_health.txt"

del "%TEMP%\stress_health.txt" >nul 2>nul

if "%HTTP_CODE%"=="200" goto READY

if %COUNT% GEQ 60 goto FAILED

timeout /t 1 /nobreak >nul

goto WAIT

REM ------------------------------------------------
REM Backend ready
REM ------------------------------------------------

:READY

echo.
echo ============================================
echo       BACKEND READY
echo ============================================
echo.
echo [OK] HTTP health check returned 200.
echo [OK] Backend is running.
echo.
echo Opening dashboard...
echo.

start "" "%URL%"

echo Dashboard:
echo %URL%
echo.
echo Stress Monitor is running.
echo.
echo You can close this window.
echo.
pause
exit /b 0

REM ------------------------------------------------
REM Backend failed
REM ------------------------------------------------

:FAILED

echo.
echo ============================================
echo       BACKEND DID NOT START
echo ============================================
echo.
echo Check these files:
echo.
echo %ROOT%.stress_monitor_backend_error.log
echo %ROOT%.stress_monitor_backend.log
echo.
echo Last backend error:
echo --------------------------------------------

if exist "%ROOT%.stress_monitor_backend_error.log" (
    type "%ROOT%.stress_monitor_backend_error.log"
)

echo.
echo --------------------------------------------
echo.
pause
exit /b 1