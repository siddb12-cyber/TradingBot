"""
runtime/windows_startup.py
===========================
Windows Task Scheduler integration for TradingBot auto-startup.

Installs two tasks:
  1. TradingBot_RuntimeManager -- runs at every user logon
  2. TradingBot_DailyStart     -- runs at 08:45 AM every weekday (Mon-Fri)

Both tasks launch start.bat from the project root with highest privileges.
Run setup_autostart.bat as Administrator once -- then the system is fully autonomous.

Usage:
    python -m runtime.windows_startup --install-all    # recommended
    python -m runtime.windows_startup --remove
    python -m runtime.windows_startup --status
"""

import logging
import subprocess
import sys
from pathlib import Path

from config.config import BASE_DIR, STARTUP_TASK_NAME

logger = logging.getLogger(__name__)

DAILY_TASK_NAME:    str = "TradingBot_DailyStart"
REMINDER_TASK_NAME: str = "TradingBot_MorningReminder"

# =========================
# SCHTASKS WRAPPER
# =========================

def _schtasks(*args: str) -> subprocess.CompletedProcess:
    cmd = ["schtasks"] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=15)


# =========================
# CHECK
# =========================

def check_startup_task_exists() -> bool:
    result = _schtasks("/Query", "/TN", STARTUP_TASK_NAME, "/FO", "LIST")
    exists = result.returncode == 0
    logger.info(f"[STARTUP] Task '{STARTUP_TASK_NAME}': {'registered' if exists else 'NOT registered'}")
    return exists


def check_daily_task_exists() -> bool:
    result = _schtasks("/Query", "/TN", DAILY_TASK_NAME, "/FO", "LIST")
    return result.returncode == 0


# =========================
# LOGON TASK
# =========================

def install_startup_task() -> bool:
    """
    Register a task that runs runtime_manager.py at every user logon.
    Must be run as Administrator.
    Note: /SD and /ST are NOT valid for ONLOGON -- omit them.
    """
    python_exe   = sys.executable
    manager_path = str(BASE_DIR / "runtime" / "runtime_manager.py")
    run_command  = f'"{python_exe}" "{manager_path}"'

    logger.info(f"[STARTUP] Installing logon task: {STARTUP_TASK_NAME}")

    result = _schtasks(
        "/Create",
        "/TN", STARTUP_TASK_NAME,
        "/TR", run_command,
        "/SC", "ONLOGON",
        "/RL", "HIGHEST",
        "/F",
        "/IT",
    )

    if result.returncode == 0:
        logger.info(f"[STARTUP] Logon task installed: {STARTUP_TASK_NAME}")
        print("  [OK] Logon task installed -- TradingBot starts at next login.")
        return True
    else:
        logger.error(f"[STARTUP] Logon task failed: {result.stderr.strip()}")
        if "Access is denied" in result.stderr or result.returncode == 1:
            print("  [ERROR] Run as Administrator.")
        return False


# =========================
# DAILY MARKET OPEN TASK
# =========================

def install_market_open_task(start_bat_path: str = None) -> bool:
    """
    Register a task that runs start.bat at 08:45 AM every weekday (Mon-Fri).
    This is the primary automation -- fires automatically every trading day.
    Must be run as Administrator once via setup_autostart.bat.
    """
    if start_bat_path is None:
        start_bat_path = str(BASE_DIR / "start.bat")

    logger.info(f"[STARTUP] Installing daily task: {DAILY_TASK_NAME} at 08:45 Mon-Fri")

    result = _schtasks(
        "/Create",
        "/TN", DAILY_TASK_NAME,
        "/TR", f'"{start_bat_path}"',
        "/SC", "WEEKLY",
        "/D",  "MON,TUE,WED,THU,FRI",
        "/ST", "08:45",
        "/RL", "HIGHEST",
        "/IT",
        "/F",
    )

    if result.returncode == 0:
        logger.info(f"[STARTUP] Daily task installed: {DAILY_TASK_NAME}")
        print("  [OK] Daily task installed -- TradingBot auto-starts at 08:45 AM Mon-Fri.")
        return True
    else:
        logger.error(f"[STARTUP] Daily task failed: {result.stderr.strip()}")
        if "Access is denied" in result.stderr or result.returncode == 1:
            print("  [ERROR] Run as Administrator.")
        return False


# =========================
# TELEGRAM REMINDER TASK
# =========================

def install_reminder_task() -> bool:
    """
    Register a task that sends a Telegram reminder at 08:30 AM every weekday.
    Runs runtime/telegram_reminder.py directly via Python.
    Must be run as Administrator.
    """
    python_exe    = sys.executable
    reminder_path = str(BASE_DIR / "runtime" / "telegram_reminder.py")
    run_command   = f'"{python_exe}" "{reminder_path}"'

    logger.info(f"[STARTUP] Installing reminder task: {REMINDER_TASK_NAME} at 08:30 Mon-Fri")

    result = _schtasks(
        "/Create",
        "/TN", REMINDER_TASK_NAME,
        "/TR", run_command,
        "/SC", "WEEKLY",
        "/D",  "MON,TUE,WED,THU,FRI",
        "/ST", "08:30",
        "/RL", "HIGHEST",
        "/IT",
        "/F",
    )

    if result.returncode == 0:
        logger.info(f"[STARTUP] Reminder task installed: {REMINDER_TASK_NAME}")
        print("  [OK] Reminder task installed -- Telegram message fires at 08:30 AM Mon-Fri.")
        return True
    else:
        logger.error(f"[STARTUP] Reminder task failed: {result.stderr.strip()}")
        if "Access is denied" in result.stderr or result.returncode == 1:
            print("  [ERROR] Run as Administrator.")
        return False


def remove_reminder_task() -> bool:
    result = _schtasks("/Delete", "/TN", REMINDER_TASK_NAME, "/F")
    if result.returncode == 0:
        logger.info(f"[STARTUP] Reminder task removed.")
        print("  [OK] Reminder task removed.")
        return True
    logger.warning(f"[STARTUP] Could not remove reminder task: {result.stderr.strip()}")
    return False


# =========================
# INSTALL ALL / REMOVE ALL
# =========================

def install_all_tasks(start_bat_path: str = None) -> bool:
    """Install logon task, daily 08:45 start task, and 08:30 Telegram reminder."""
    print("\n[STARTUP] Installing all TradingBot scheduled tasks...\n")
    ok1 = install_startup_task()
    ok2 = install_market_open_task(start_bat_path)
    ok3 = install_reminder_task()
    if ok1 and ok2 and ok3:
        print("\n[STARTUP] All tasks installed successfully.")
        print("[STARTUP] TradingBot schedule:")
        print("           - 08:30 AM Mon-Fri: Telegram morning reminder")
        print("           - 08:45 AM Mon-Fri: System auto-start")
        print("           - At every Windows logon: Runtime watchdog")
    else:
        print("\n[STARTUP] One or more tasks failed -- check errors above.")
    return ok1 and ok2 and ok3


def remove_startup_task() -> bool:
    result = _schtasks("/Delete", "/TN", STARTUP_TASK_NAME, "/F")
    if result.returncode == 0:
        logger.info(f"[STARTUP] Logon task removed.")
        print("  [OK] Logon task removed.")
        return True
    logger.warning(f"[STARTUP] Could not remove logon task: {result.stderr.strip()}")
    return False


def remove_market_open_task() -> bool:
    result = _schtasks("/Delete", "/TN", DAILY_TASK_NAME, "/F")
    if result.returncode == 0:
        logger.info(f"[STARTUP] Daily task removed.")
        print("  [OK] Daily task removed.")
        return True
    logger.warning(f"[STARTUP] Could not remove daily task: {result.stderr.strip()}")
    return False


def remove_all_tasks() -> bool:
    print("\n[STARTUP] Removing all TradingBot scheduled tasks...\n")
    ok1 = remove_startup_task()
    ok2 = remove_market_open_task()
    ok3 = remove_reminder_task()
    return ok1 and ok2 and ok3


# =========================
# STATUS REPORT
# =========================

def print_startup_status() -> None:
    print(f"\n{'=' * 55}")
    print("  TradingBot -- Windows Scheduled Tasks Status")
    print(f"{'=' * 55}")
    for task_name, label in [
        (REMINDER_TASK_NAME, "Reminder task (Telegram at 08:30)"),
        (STARTUP_TASK_NAME,  "Logon task   (runtime_manager.py)"),
        (DAILY_TASK_NAME,    "Daily task   (start.bat at 08:45)"),
    ]:
        result = _schtasks("/Query", "/TN", task_name, "/FO", "LIST")
        if result.returncode == 0:
            print(f"\n  [INSTALLED] {label}")
            for line in result.stdout.splitlines():
                if any(k in line for k in ["Task Name", "Status", "Next Run"]):
                    print(f"    {line.strip()}")
        else:
            print(f"\n  [MISSING]   {label}")
    print(f"\n{'=' * 55}")
    print("  Run setup_autostart.bat as Administrator to install.")
    print(f"{'=' * 55}\n")


# =========================
# STANDALONE ENTRY
# =========================

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="TradingBot Windows startup manager")
    parser.add_argument("--install-all", action="store_true",
                        help="Install logon + daily 08:45 tasks (recommended)")
    parser.add_argument("--install",     action="store_true",
                        help="Install logon task only")
    parser.add_argument("--remove",      action="store_true",
                        help="Remove all tasks")
    parser.add_argument("--status",      action="store_true",
                        help="Show task status")
    args = parser.parse_args()

    if args.install_all:
        install_all_tasks()
    elif args.install:
        install_startup_task()
    elif args.remove:
        remove_all_tasks()
    else:
        print_startup_status()
