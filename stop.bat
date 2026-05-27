@echo off
REM ============================================================
REM stop.bat
REM TradingBot — Stop All Bot Processes
REM
REM Double-click this to stop the bot cleanly.
REM Kills all pythonw.exe instances running TradingBot.
REM ============================================================

echo Stopping TradingBot...

REM Kill all pythonw.exe processes (the bot runs as pythonw)
taskkill /F /IM pythonw.exe /T 2>nul

if %ERRORLEVEL% EQU 0 (
    echo Bot stopped successfully.
) else (
    echo No bot process found (already stopped).
)

echo.
echo Press any key to close...
pause >nul
