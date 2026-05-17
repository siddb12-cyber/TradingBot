@echo off
:: ============================================================
:: TradingBot — One-Click Startup
:: Double-click this file every morning before 9:15 AM IST
:: ============================================================

title TradingBot Startup
color 0A

echo.
echo ============================================================
echo   TradingBot - Autonomous Trading System
echo   Starting up...
echo ============================================================
echo.

:: --- Move to project root (same folder as this .bat file) ---
cd /d "%~dp0"

:: --- Step 1: Install / update pip dependencies ---
echo [1/3] Checking pip dependencies...
python -m pip install -r requirements.txt --quiet
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] pip install failed. Check your Python installation.
    pause
    exit /b 1
)
echo       Done.
echo.

:: --- Step 2: Pre-flight check (auto-fixes ports, dirs, etc.) ---
echo [2/3] Running pre-flight checks...
python runtime/preflight_check.py
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [BLOCKED] Pre-flight check failed. Fix the errors above and re-run.
    echo.
    pause
    exit /b 1
)
echo.

:: --- Step 3: Launch Runtime Manager ---
echo [3/3] Launching TradingBot Runtime Manager...
echo       (Chrome will open automatically)
echo       (Press Ctrl+C in this window to stop everything)
echo.
echo ============================================================
echo   SYSTEM STARTING — Watch Telegram for confirmation
echo ============================================================
echo.

python runtime/runtime_manager.py

:: --- If runtime_manager exits, pause so user can read error ---
echo.
echo [INFO] Runtime Manager has stopped.
pause
