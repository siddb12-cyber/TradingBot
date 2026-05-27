@echo off
REM ============================================================
REM start.bat
REM TradingBot — ONE DOUBLE-CLICK MORNING LAUNCHER
REM
REM This is ALL you need to do every morning:
REM   1. Double-click this file
REM   2. Bot starts in background (no visible window)
REM   3. Check Telegram for startup confirmation
REM
REM To stop: double-click stop.bat
REM To view dashboard: double-click dashboard\start_dashboard.bat
REM
REM Logs: TradingBot\logs\trading.log
REM ============================================================

cd /d C:\Users\siddh\Downloads\HK\TradingBot

REM Create logs directory if missing
if not exist logs mkdir logs

REM Set UTF-8 encoding
set PYTHONIOENCODING=utf-8

REM ── Auto-restart loop ──────────────────────────────────────────────────────
REM pythonw.exe = no console window (silent background)
REM The loop restarts on crash; exits cleanly on exit code 0 (stop.bat)
:LOOP
pythonw.exe -X utf8 main.py >> logs\trading.log 2>&1
if %ERRORLEVEL% EQU 0 goto DONE
echo [RESTART] Bot exited with code %ERRORLEVEL%. Restarting in 15 seconds... >> logs\trading.log
timeout /t 15 /nobreak >nul
goto LOOP

:DONE
echo [STOPPED] Bot stopped cleanly. >> logs\trading.log
