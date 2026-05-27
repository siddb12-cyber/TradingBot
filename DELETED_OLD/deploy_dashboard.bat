@echo off
:: ============================================================
:: TradingBot — Deploy Dashboard to GitHub Pages
:: Run this any time you want to push the latest dashboard live.
:: Auto-runs at EOD via paper_trading_report.py as well.
:: ============================================================

title TradingBot Dashboard Deploy
color 0B

cd /d "%~dp0"

echo.
echo [1/3] Generating dashboard from latest trade data...
python -m analytics.dashboard_generator
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Dashboard generation failed.
    pause
    exit /b 1
)

echo.
echo [2/3] Staging changes for GitHub...
git add docs/index.html docs/.nojekyll trade_logs/ data/
git status --short

echo.
echo [3/3] Committing and pushing to GitHub...
for /f "tokens=1-3 delims=/ " %%a in ('date /t') do set DATE_STR=%%c-%%b-%%a
for /f "tokens=1-2 delims=: " %%a in ('time /t') do set TIME_STR=%%a:%%b
git commit -m "chore: dashboard update %DATE_STR% %TIME_STR%"
git push origin main

if %ERRORLEVEL% EQU 0 (
    echo.
    echo [OK] Dashboard pushed to GitHub.
    echo      Live at: https://siddb12-cyber.github.io/TradingBot/
    echo      (GitHub Pages takes ~60 seconds to update)
) else (
    echo [WARN] Push failed - check git credentials or network.
)

echo.
pause
