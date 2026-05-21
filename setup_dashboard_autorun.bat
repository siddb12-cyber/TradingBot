@echo off
:: ============================================================
:: setup_dashboard_autorun.bat
:: Run ONCE to register daily dashboard auto-generation at 15:35.
:: Task runs Monday–Friday, 5 minutes after market close (15:30).
:: ============================================================

echo Setting up TradingBot_DashboardAutoRun scheduled task...

:: Delete old task if it exists (clean re-register)
schtasks /delete /tn "TradingBot_DashboardAutoRun" /f >nul 2>&1

:: Register new task — weekdays at 15:35
schtasks /create ^
  /tn "TradingBot_DashboardAutoRun" ^
  /tr "\"C:\Users\siddh\Downloads\HK\TradingBot\run_dashboard.bat\"" ^
  /sc WEEKLY ^
  /d MON,TUE,WED,THU,FRI ^
  /st 15:35 ^
  /f

if %ERRORLEVEL% EQU 0 (
    echo.
    echo SUCCESS: Dashboard will auto-generate every weekday at 15:35 IST.
    echo Log file: C:\Users\siddh\Downloads\HK\TradingBot\logs\dashboard_autorun.log
) else (
    echo.
    echo ERROR: Task registration failed. Try running as Administrator.
)

pause
