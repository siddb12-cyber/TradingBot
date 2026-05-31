@echo off
REM ============================================================
REM  TradingBot — One-click setup for a new Windows machine
REM  Run this ONCE after cloning the repo.
REM ============================================================

echo.
echo  ===  TradingBot Setup  ===
echo.

REM 1. Check Python
python --version >nul 2>&1
IF ERRORLEVEL 1 (
    echo  [ERROR] Python not found. Install Python 3.10+ from https://python.org
    echo          Make sure to check "Add Python to PATH" during install.
    pause
    exit /b 1
)
echo  [OK] Python found

REM 2. Install dependencies
echo  [..] Installing Python packages...
pip install -r requirements.txt --quiet
IF ERRORLEVEL 1 (
    echo  [ERROR] pip install failed. Check your internet connection.
    pause
    exit /b 1
)
echo  [OK] Packages installed

REM 3. Create .env from template if it doesn't exist
IF NOT EXIST .env (
    copy .env.example .env >nul
    echo  [!!] Created .env from template.
    echo       Open .env and fill in your BOT_TOKEN and CHAT_ID before starting.
) ELSE (
    echo  [OK] .env already exists
)

REM 4. Create runtime directories
IF NOT EXIST data       mkdir data
IF NOT EXIST logs       mkdir logs
IF NOT EXIST decisions  mkdir decisions
IF NOT EXIST trades     mkdir trades
echo  [OK] Runtime directories ready

echo.
echo  ===  Setup complete!  ===
echo.
echo  Next steps:
echo    1. Edit .env  — add your Telegram BOT_TOKEN and CHAT_ID
echo    2. Run start.bat to launch the bot
echo    3. Send /ping to your Telegram bot to verify it's alive
echo.
pause
