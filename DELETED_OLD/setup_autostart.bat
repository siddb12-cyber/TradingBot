@echo off
REM ============================================================
REM setup_autostart.bat
REM TradingBot Windows Auto-Start Setup
REM
REM Run this ONCE. After that, TradingBot starts silently
REM every time you log into Windows — no terminal, no clicks.
REM
REM Does NOT require Admin rights.
REM Uses Windows Task Scheduler (current user only).
REM ============================================================

echo.
echo  ============================================================
echo   TradingBot Auto-Start Setup
echo  ============================================================
echo.

set BOT_DIR=C:\Users\siddh\Downloads\HK\TradingBot
set TASK_NAME=TradingBot_AutoStart
set VBS_PATH=%BOT_DIR%\start_hidden.vbs

REM ---- Check the VBS launcher exists ----
if not exist "%VBS_PATH%" (
    echo  ERROR: start_hidden.vbs not found at:
    echo  %VBS_PATH%
    echo  Please run this from the TradingBot folder.
    pause
    exit /b 1
)

REM ---- Remove old task if it exists ----
schtasks /delete /tn "%TASK_NAME%" /f >nul 2>&1

REM ---- Register new Task Scheduler task ----
REM Trigger: At login of current user
REM Action : Run start_hidden.vbs (which launches pythonw.exe silently)
REM Delay  : 60 seconds after login (gives Windows time to settle)
REM RunAs  : Current user (no admin needed)

schtasks /create ^
  /tn "%TASK_NAME%" ^
  /tr "wscript.exe \"%VBS_PATH%\"" ^
  /sc ONLOGON ^
  /delay 0001:00 ^
  /it ^
  /f

if %ERRORLEVEL% EQU 0 (
    echo.
    echo  SUCCESS! TradingBot will now auto-start on every login.
    echo.
    echo  Details:
    echo    Task name : %TASK_NAME%
    echo    Trigger   : At login (60s delay)
    echo    Launches  : start_hidden.vbs (no terminal window)
    echo    Logs      : %BOT_DIR%\logs\trading.log
    echo.
    echo  To remove auto-start later, run:
    echo    schtasks /delete /tn "%TASK_NAME%" /f
    echo.
) else (
    echo.
    echo  ERROR: Task Scheduler registration failed.
    echo  Try running this file as Administrator.
    echo.
)

pause
