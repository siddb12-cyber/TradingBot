@echo off
REM ============================================================
REM start_dashboard.bat
REM TradingBot Dashboard Launcher
REM
REM Double-click to open live dashboard at http://localhost:5050
REM Runs in a visible window so you can see any errors.
REM ============================================================

title TradingBot Dashboard
cd /d C:\Users\siddh\Downloads\HK\TradingBot

echo Starting TradingBot Dashboard...
echo Open your browser at: http://localhost:5050
echo Press Ctrl+C to stop the dashboard.
echo.

python dashboard\server.py

pause
