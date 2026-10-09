@echo off
setlocal EnableExtensions
title Stress Monitor - Stopping...
cd /d "%~dp0"

set "PIDFILE=%CD%\.stress_monitor_backend.pid"

echo ============================================
echo   Stress Monitor - Stop
echo ============================================
echo.

if not exist "%PIDFILE%" (
    echo No record of a running Stress Monitor backend was found.
    echo ^(.stress_monitor_backend.pid does not exist^)
    echo.
    echo If a "Stress Monitor Backend" window is still open, you can
    echo just close that window directly.
    echo.
    pause
    exit /b 0
)

set /p BACKEND_PID=<"%PIDFILE%"

if "%BACKEND_PID%"=="" (
    echo The PID file was empty, nothing to stop.
    del "%PIDFILE%" >nul 2>nul
    pause
    exit /b 0
)

echo Stopping backend process ^(PID %BACKEND_PID%^) ...
taskkill /PID %BACKEND_PID% /T /F >nul 2>nul

if %ERRORLEVEL%==0 (
    echo Backend stopped.
) else (
    echo The process was not running ^(it may have already stopped^).
)

del "%PIDFILE%" >nul 2>nul

echo.
echo Done. It is now safe to close this window.
echo ============================================
echo.
pause
exit /b 0
