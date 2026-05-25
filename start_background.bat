@echo off
REM ============================================================
REM start_background.bat
REM TradingBot Background Launcher
REM
REM Uses pythonw.exe to run with NO console window.
REM stdout and stderr are redirected to logs\trading.log
REM
REM Called by start_hidden.vbs — do not run this directly
REM if you want a hidden window (it will flash briefly).
REM ============================================================

cd /d C:\Users\siddh\Downloads\HK\TradingBot

REM Create logs folder if it doesn't exist
if not exist logs mkdir logs

REM Clear old log on fresh start (optional — comment out to keep history)
REM del /f /q logs\trading.log 2>nul

REM Set UTF-8 encoding so emojis in Telegram messages don't crash the log redirect
set PYTHONIOENCODING=utf-8

REM Start bot with pythonw.exe (no console window)
REM Redirect both stdout and stderr to the log file
pythonw.exe -X utf8 main.py >> logs\trading.log 2>&1
