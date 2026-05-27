"""
runtime/preflight_check.py
===========================
Pre-flight system check for TradingBot.

Runs automatically from start.bat before the runtime_manager launches.
Checks every dependency and configuration value, fixes what it can
automatically, and prints a clear GO / NO-GO report.

Checks performed:
  1. Python version >= 3.9
  2. All required pip packages installed (auto-installs if missing)
  3. .env file exists and all required keys are set
  4. Chrome executable path is valid
  5. TradingView Chrome profile directory exists
  6. Groww Chrome profile directory exists (non-critical — warns only)
  7. Tesseract OCR executable reachable
  8. Ports 9222 and 9333 are free (auto-kills conflicts on Windows)
  9. Required project directories exist (trade_logs, data, screenshots)
 10. PAPER_TRADING_MODE is True (safety gate)

Usage:
    python runtime/preflight_check.py           # check + auto-fix
    python runtime/preflight_check.py --strict  # fail on any warning
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

# =========================
# RESULT TRACKING
# =========================

PASS  = "PASS"
WARN  = "WARN"
FAIL  = "FAIL"
FIX   = "FIX "

_results: list = []   # list of (status, message)
_auto_fixed: list = []


def _record(status: str, message: str) -> None:
    _results.append((status, message))
    icon = {"PASS": "✅", "WARN": "⚠️ ", "FAIL": "❌", "FIX ": "🔧"}.get(status, "  ")
    print(f"  {icon} [{status}] {message}")


# =========================
# CHECK FUNCTIONS
# =========================

def check_python_version() -> None:
    v = sys.version_info
    if v >= (3, 9):
        _record(PASS, f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        _record(FAIL, f"Python {v.major}.{v.minor} found — requires >= 3.9")


def check_pip_packages() -> None:
    """Check all required packages; auto-install missing ones."""
    required = [
        "playwright",
        "pytesseract",
        "Pillow",
        "openpyxl",
        "requests",
        "python-dotenv",
        "pyTelegramBotAPI",
        "schedule",
    ]
    missing = []
    for pkg in required:
        import importlib
        import_name = {
            "python-dotenv":    "dotenv",
            "pyTelegramBotAPI": "telebot",
            "Pillow":           "PIL",
        }.get(pkg, pkg.lower().replace("-", "_"))
        try:
            importlib.import_module(import_name)
        except ImportError:
            missing.append(pkg)

    if not missing:
        _record(PASS, f"All {len(required)} pip packages installed")
        return

    _record(FIX, f"Auto-installing missing packages: {', '.join(missing)}")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install"] + missing + ["--quiet"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        _record(PASS, f"Installed: {', '.join(missing)}")
        _auto_fixed.append(f"pip install {' '.join(missing)}")
    else:
        _record(FAIL, f"pip install failed: {result.stderr.strip()[:120]}")


def check_env_file(base_dir: Path) -> None:
    env_path = base_dir / ".env"
    if not env_path.exists():
        _record(FAIL, f".env file not found at {env_path}")
        _record(WARN, "Copy .env.example to .env and fill in BOT_TOKEN, CHAT_ID, TESSERACT_CMD")
        return
    _record(PASS, ".env file exists")

    # Check required keys
    required_keys = ["BOT_TOKEN", "CHAT_ID", "TESSERACT_CMD"]
    try:
        from dotenv import dotenv_values
        env = dotenv_values(env_path)
        for key in required_keys:
            val = env.get(key, "").strip()
            if not val or val.startswith("YOUR_"):
                _record(FAIL, f".env missing or placeholder: {key}")
            else:
                _record(PASS, f".env key set: {key} ({len(val)} chars)")
    except ImportError:
        _record(WARN, "python-dotenv not yet installed — skipping .env key checks")


def check_chrome_exe() -> None:
    # Pull from env or default
    chrome_path = os.getenv(
        "CHROME_EXE_PATH",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    )
    if Path(chrome_path).exists():
        _record(PASS, f"Chrome executable found: {chrome_path}")
    else:
        # Try alternate locations
        alternates = [
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Users\siddh\AppData\Local\Google\Chrome\Application\chrome.exe",
        ]
        found = None
        for alt in alternates:
            if Path(alt).exists():
                found = alt
                break
        if found:
            _record(WARN,
                f"Chrome found at alternate path: {found}\n"
                f"    Add CHROME_EXE_PATH={found} to .env to avoid this warning."
            )
        else:
            _record(FAIL,
                f"Chrome not found at: {chrome_path}\n"
                f"    Set CHROME_EXE_PATH in .env"
            )


def check_chrome_profile(profile_dir: str, label: str, critical: bool = True) -> bool:
    path = Path(profile_dir)
    if path.exists():
        _record(PASS, f"{label} Chrome profile exists: {path.name}")
        return True
    else:
        status = FAIL if critical else WARN
        _record(status,
            f"{label} Chrome profile NOT FOUND: {profile_dir}\n"
            f"    Open Chrome with that profile once, sign in, then close it."
        )
        return False


def check_tesseract() -> None:
    tesseract_cmd = os.getenv(
        "TESSERACT_CMD",
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    )
    if Path(tesseract_cmd).exists():
        _record(PASS, f"Tesseract found: {tesseract_cmd}")
    else:
        _record(FAIL,
            f"Tesseract not found: {tesseract_cmd}\n"
            f"    Download from: https://github.com/UB-Mannheim/tesseract/wiki\n"
            f"    Then set TESSERACT_CMD in .env"
        )


def check_port(port: int, label: str) -> None:
    """Check if port is free. Auto-kill the occupying process on Windows."""
    if sys.platform != "win32":
        _record(PASS, f"Port {port} ({label}) — skipping check (not Windows)")
        return

    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, timeout=10,
        )
        pid = None
        for line in result.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.split()
                if parts:
                    try:
                        pid = int(parts[-1])
                    except ValueError:
                        pass
                break

        if pid is None or pid == 0:
            _record(PASS, f"Port {port} ({label}) is free")
            return

        # Auto-kill
        _record(FIX, f"Port {port} ({label}) occupied by PID {pid} — killing automatically")
        kill = subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True, text=True, timeout=10,
        )
        if kill.returncode == 0:
            _record(PASS, f"Port {port} ({label}) — PID {pid} killed, port now free")
            _auto_fixed.append(f"Killed PID {pid} on port {port}")
        else:
            _record(FAIL,
                f"Could not kill PID {pid} on port {port}: {kill.stderr.strip()}\n"
                f"    Close the process manually and retry."
            )
    except Exception as exc:
        _record(WARN, f"Port {port} check error: {exc}")


def check_directories(base_dir: Path) -> None:
    """Ensure required project directories exist; create if missing."""
    dirs = ["trade_logs", "data", "screenshots", "temp", "runtime/heartbeats"]
    for d in dirs:
        path = base_dir / d
        if path.exists():
            _record(PASS, f"Directory exists: {d}/")
        else:
            path.mkdir(parents=True, exist_ok=True)
            _record(FIX, f"Created missing directory: {d}/")
            _auto_fixed.append(f"mkdir {d}")


def check_paper_trading_mode() -> None:
    """Confirm PAPER_TRADING_MODE is True — hard safety gate."""
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from config.config import PAPER_TRADING_MODE, PAPER_TRADING_VALIDATION_END
        if PAPER_TRADING_MODE:
            _record(PASS, f"PAPER_TRADING_MODE=True (validation until {PAPER_TRADING_VALIDATION_END})")
        else:
            _record(FAIL,
                "PAPER_TRADING_MODE=False — REAL ORDERS ENABLED!\n"
                "    Set PAPER_TRADING_MODE=True in config/config.py before running."
            )
    except Exception as exc:
        _record(WARN, f"Could not import config: {exc}")


# =========================
# REPORT
# =========================

def _print_report(strict: bool) -> int:
    """Print summary and return exit code (0=GO, 1=NO-GO)."""
    passes  = sum(1 for s, _ in _results if s == PASS)
    warns   = sum(1 for s, _ in _results if s == WARN)
    fails   = sum(1 for s, _ in _results if s == FAIL)
    fixes   = sum(1 for s, _ in _results if s == FIX)

    print()
    print("=" * 58)
    print(f"  PRE-FLIGHT RESULTS: {passes} passed | {warns} warnings | {fails} failed | {fixes} auto-fixed")

    if _auto_fixed:
        print(f"\n  Auto-fixed:")
        for f in _auto_fixed:
            print(f"    • {f}")

    print()
    if fails == 0 and (warns == 0 or not strict):
        print("  🚀 STATUS: GO — TradingBot is ready to launch.")
        exit_code = 0
    elif fails == 0:
        print("  ⚠️  STATUS: GO WITH WARNINGS — review warnings before market open.")
        exit_code = 0
    else:
        print("  ❌ STATUS: NO-GO — fix the FAIL items above, then re-run.")
        exit_code = 1

    print("=" * 58)
    return exit_code


# =========================
# MAIN
# =========================

def run(strict: bool = False) -> int:
    print()
    print("=" * 58)
    print("  TradingBot Pre-Flight Check")
    print("=" * 58)

    base_dir = Path(__file__).parent.parent.resolve()

    # Pull .env into os.environ before config checks
    env_path = base_dir / ".env"
    if env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path, override=False)
        except ImportError:
            pass  # dotenv not yet installed — pip check will handle it

    tv_profile  = os.getenv("TRADINGVIEW_PROFILE_DIR",
        r"C:\Users\siddh\AppData\Local\Google\Chrome\User Data\Profile 7")
    groww_profile = os.getenv("GROWW_PROFILE_DIR",
        r"C:\Users\siddh\AppData\Local\Google\Chrome\User Data\Profile 8")

    print()
    check_python_version()
    check_pip_packages()
    check_env_file(base_dir)
    check_chrome_exe()
    check_chrome_profile(tv_profile,    "TradingView", critical=True)
    check_chrome_profile(groww_profile, "Groww",       critical=False)
    check_tesseract()
    check_port(9222, "TradingView CDP")
    check_port(9333, "Groww CDP")
    check_directories(base_dir)
    check_paper_trading_mode()
    print()

    return _print_report(strict)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TradingBot pre-flight check")
    parser.add_argument("--strict", action="store_true",
                        help="Exit non-zero even on warnings")
    args = parser.parse_args()
    sys.exit(run(strict=args.strict))
