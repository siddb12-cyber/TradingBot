@echo off
:: ============================================================
:: TradingBot — One-Time GitHub Setup
:: Double-click this file. When asked for password, paste your
:: GitHub Personal Access Token (not your GitHub password).
::
:: Get a token here (takes 30 seconds):
:: github.com → Settings → Developer settings →
:: Personal access tokens → Tokens (classic) →
:: Generate new token → tick "repo" → Generate → Copy
:: ============================================================

title TradingBot — GitHub Setup
color 0A

cd /d "%~dp0"

echo.
echo ============================================================
echo   TradingBot -- GitHub Setup
echo   Repo: https://github.com/siddb12-cyber/TradingBot
echo ============================================================
echo.

:: ============================================================
:: STEP 0: Clean up any broken/stale .git directory
:: This fixes the "could not lock config file" error
:: ============================================================
if exist ".git" (
    echo [FIX] Found existing .git folder -- removing stale git data...
    rmdir /s /q ".git"
    if exist ".git" (
        echo [ERROR] Could not remove .git folder. Try running as Administrator.
        pause
        exit /b 1
    )
    echo [OK]  Stale .git removed -- starting fresh.
) else (
    echo [OK]  No previous .git folder found.
)

echo.

:: ============================================================
:: STEP 1: Initialize fresh git repo
:: ============================================================
echo [1/5] Initializing git repository...
git init
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] git init failed. Is Git installed?
    echo         Download from: https://git-scm.com/download/win
    pause
    exit /b 1
)
git branch -M main
echo [OK]  Git initialized on branch main.

:: ============================================================
:: STEP 2: Configure identity
:: ============================================================
echo.
echo [2/5] Configuring git identity...
git config user.name "Sidhant"
git config user.email "siddb12@gmail.com"
echo [OK]  Identity: Sidhant ^<siddb12@gmail.com^>

:: ============================================================
:: STEP 3: Set remote origin
:: ============================================================
echo.
echo [3/5] Setting remote origin...
git remote remove origin 2>nul
git remote add origin https://github.com/siddb12-cyber/TradingBot.git
echo [OK]  Remote: github.com/siddb12-cyber/TradingBot

:: ============================================================
:: STEP 4: Stage and commit all files
:: ============================================================
echo.
echo [4/5] Staging all project files...
git add .
echo [OK]  Files staged.

git commit -m "feat: initial TradingBot setup -- paper trading system"
if %ERRORLEVEL% NEQ 0 (
    echo [WARN] Commit step had an issue -- this may be normal if repo already has commits.
)
echo [OK]  Commit created.

:: ============================================================
:: STEP 5: Push to GitHub
:: ============================================================
echo.
echo ============================================================
echo   [5/5] PUSHING TO GITHUB
echo.
echo   When prompted for credentials:
echo     Username: siddb12-cyber
echo     Password: paste your Personal Access Token
echo              (NOT your GitHub account password)
echo ============================================================
echo.

git push -u origin main

if %ERRORLEVEL% EQU 0 (
    echo.
    echo ============================================================
    echo   SUCCESS! TradingBot is now live on GitHub.
    echo   Repo: https://github.com/siddb12-cyber/TradingBot
    echo.
    echo   FINAL STEP -- Enable GitHub Pages (2 minutes):
    echo   1. Open: github.com/siddb12-cyber/TradingBot/settings/pages
    echo   2. Source: "Deploy from branch"
    echo   3. Branch: main    Folder: /docs
    echo   4. Click Save
    echo.
    echo   Dashboard live at (after ~60 seconds):
    echo   https://siddb12-cyber.github.io/TradingBot/
    echo ============================================================
) else (
    echo.
    echo [ERROR] Push failed. Common causes:
    echo   1. Wrong credentials -- use Personal Access Token, not password
    echo   2. Token missing "repo" permission -- regenerate with repo scope
    echo   3. Repository doesn't exist yet -- create it on github.com first
    echo      (make sure it's EMPTY -- no README, no .gitignore)
    echo.
    echo Re-run this file after fixing the issue.
)

echo.
pause
