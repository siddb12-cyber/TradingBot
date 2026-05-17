# ============================================================
# TradingBot — GitHub Setup (PowerShell)
# Right-click this file → "Run with PowerShell"
# ============================================================

$ErrorActionPreference = "Continue"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  TradingBot -- GitHub Setup" -ForegroundColor Cyan
Write-Host "  Repo: https://github.com/siddb12-cyber/TradingBot" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# ============================================================
# STEP 0: Kill any stuck git processes, then wipe stale .git
# ============================================================
Write-Host "[CLEAN] Stopping any running git processes..." -ForegroundColor Yellow
Get-Process -Name "git" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1

$gitDir = Join-Path $ProjectDir ".git"
if (Test-Path $gitDir) {
    Write-Host "[CLEAN] Removing stale .git folder..." -ForegroundColor Yellow
    # Delete all lock files first
    Get-ChildItem -Path $gitDir -Filter "*.lock" -Recurse -Force -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
    # Now remove the whole directory
    Remove-Item -Path $gitDir -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path $gitDir) {
        Write-Host "[WARN]  Could not fully remove .git -- will attempt to reinitialize anyway." -ForegroundColor Yellow
    } else {
        Write-Host "[OK]   Stale .git removed." -ForegroundColor Green
    }
} else {
    Write-Host "[OK]   No stale .git found." -ForegroundColor Green
}

Write-Host ""

# ============================================================
# STEP 1: git init
# ============================================================
Write-Host "[1/5] Initializing git repository..." -ForegroundColor White
$result = & git init 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] git init failed: $result" -ForegroundColor Red
    Write-Host "        Is Git installed? Download: https://git-scm.com/download/win" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}
& git branch -M main 2>&1 | Out-Null
Write-Host "[OK]   Git initialized on branch: main" -ForegroundColor Green

# ============================================================
# STEP 2: Set identity
# ============================================================
Write-Host ""
Write-Host "[2/5] Setting git identity..." -ForegroundColor White
& git config user.name "Sidhant"
& git config user.email "siddb12@gmail.com"
Write-Host "[OK]   Identity: Sidhant <siddb12@gmail.com>" -ForegroundColor Green

# ============================================================
# STEP 3: Set remote
# ============================================================
Write-Host ""
Write-Host "[3/5] Setting remote origin..." -ForegroundColor White
& git remote remove origin 2>&1 | Out-Null
& git remote add origin https://github.com/siddb12-cyber/TradingBot.git
Write-Host "[OK]   Remote: github.com/siddb12-cyber/TradingBot" -ForegroundColor Green

# ============================================================
# STEP 4: Stage and commit
# ============================================================
Write-Host ""
Write-Host "[4/5] Staging all project files..." -ForegroundColor White
& git add . 2>&1
Write-Host "[OK]   Files staged." -ForegroundColor Green

Write-Host "      Creating commit..." -ForegroundColor White
& git commit -m "feat: initial TradingBot setup -- paper trading system" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "[WARN]  Commit had an issue (may already have commits -- continuing)." -ForegroundColor Yellow
} else {
    Write-Host "[OK]   Commit created." -ForegroundColor Green
}

# ============================================================
# STEP 5: Clear cached GitHub credentials, then push
# ============================================================
Write-Host ""
Write-Host "[5/5] Clearing any cached GitHub credentials..." -ForegroundColor White

# Remove ALL cached github.com entries from Windows Credential Manager
# This forces Git to prompt fresh — using the correct siddb12-cyber account
$credTargets = @(
    "git:https://github.com",
    "git:https://siddb12-cyber@github.com",
    "git:https://hausandkinder-ops@github.com",
    "https://github.com"
)
foreach ($target in $credTargets) {
    & cmdkey /delete:$target 2>&1 | Out-Null
}
Write-Host "[OK]   Cached credentials cleared." -ForegroundColor Green

# Embed username in remote URL so Git knows which account to use
& git remote set-url origin https://siddb12-cyber@github.com/siddb12-cyber/TradingBot.git
Write-Host "[OK]   Remote URL updated with username." -ForegroundColor Green

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  PUSHING TO GITHUB" -ForegroundColor Cyan
Write-Host ""
Write-Host "  You will be prompted for a password." -ForegroundColor White
Write-Host "  Paste your Personal Access Token (NOT your GitHub password)." -ForegroundColor Yellow
Write-Host ""
Write-Host "  Don't have a token? Get one in 30 seconds:" -ForegroundColor Gray
Write-Host "  github.com -> Settings -> Developer settings ->" -ForegroundColor Gray
Write-Host "  Personal access tokens -> Tokens (classic) ->" -ForegroundColor Gray
Write-Host "  Generate new token -> tick 'repo' -> Generate -> Copy" -ForegroundColor Gray
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

& git push -u origin main

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host "  SUCCESS! TradingBot is live on GitHub." -ForegroundColor Green
    Write-Host "  Repo: https://github.com/siddb12-cyber/TradingBot" -ForegroundColor Green
    Write-Host ""
    Write-Host "  FINAL STEP -- Enable GitHub Pages:" -ForegroundColor White
    Write-Host "  1. Open: github.com/siddb12-cyber/TradingBot/settings/pages" -ForegroundColor White
    Write-Host "  2. Source: 'Deploy from branch'" -ForegroundColor White
    Write-Host "  3. Branch: main   Folder: /docs" -ForegroundColor White
    Write-Host "  4. Click Save" -ForegroundColor White
    Write-Host ""
    Write-Host "  Dashboard live at (after ~60 seconds):" -ForegroundColor White
    Write-Host "  https://siddb12-cyber.github.io/TradingBot/" -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Red
    Write-Host "  [ERROR] Push failed. Common causes:" -ForegroundColor Red
    Write-Host "  1. Wrong credentials -- use Personal Access Token, not password" -ForegroundColor White
    Write-Host "  2. Token missing 'repo' scope -- regenerate with repo ticked" -ForegroundColor White
    Write-Host "  3. Repo not empty on GitHub -- delete all files there first" -ForegroundColor White
    Write-Host "  4. Repo doesn't exist -- create it at github.com (leave it EMPTY)" -ForegroundColor White
    Write-Host "============================================================" -ForegroundColor Red
}

Write-Host ""
Read-Host "Press Enter to close"
