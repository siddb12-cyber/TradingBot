@echo off
:: ============================================================
:: TradingBot — One-Time Autostart Setup
:: Run this file ONCE as Administrator (right-click → Run as administrator)
:: After this, TradingBot will start itself every day at 08:45 AM.
:: You never need to run this again.
:: ============================================================

title TradingBot — Autostart Setup
color 0B

echo.
echo ============================================================
echo   TradingBot — Windows Autostart Setup
echo   Run ONCE as Administrator. Then forget about it.
echo ============================================================
echo.

:: --- Check we are running as Administrator ---
net session >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] This script must be run as Administrator.
    echo.
    echo Right-click setup_autostart.bat and choose:
    echo   "Run as administrator"
    echo.
    pause
    exit /b 1
)
echo [OK] Running as Administrator.
echo.

:: --- Move to project root ---
cd /d "%~dp0"

:: --- Install Python packages first ---
echo [1/2] Installing pip dependencies...
python -m pip install -r requirements.txt --quiet
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] pip install failed. Check Python installation.
    pause
    exit /b 1
)
echo [OK] Dependencies ready.
echo.

:: --- Install both Windows scheduled tasks ---
echo [2/2] Registering Windows scheduled tasks...
python -m runtime.windows_startup --install-all
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Task registration failed. See errors above.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   SETUP COMPLETE
echo.
echo   TradingBot will now start automatically:
echo     * Every day at 08:45 AM (Mon - Fri)
echo     * At every Windows logon
echo.
echo   You do NOT need to do anything tomorrow morning.
echo   Watch Telegram at 08:45 AM for the startup notification.
echo.
echo   To verify tasks: open Task Scheduler and look for
echo     - TradingBot_DailyStart
echo     - TradingBot_RuntimeManager
echo ============================================================
echo.
pause
