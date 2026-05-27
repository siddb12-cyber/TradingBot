@echo off
:: ============================================================
:: run_dashboard.bat
:: Runs analytics/generate_dashboard.py after market close.
:: Scheduled daily at 15:35 IST via Task Scheduler.
:: ============================================================

cd /d "C:\Users\siddh\Downloads\HK\TradingBot"

echo [%date% %time%] Running dashboard generator... >> logs\dashboard_autorun.log

python analytics\generate_dashboard.py >> logs\dashboard_autorun.log 2>&1

if %ERRORLEVEL% EQU 0 (
    echo [%date% %time%] Dashboard generated successfully. >> logs\dashboard_autorun.log
) else (
    echo [%date% %time%] ERROR: Dashboard generation failed. Check above log. >> logs\dashboard_autorun.log
)
