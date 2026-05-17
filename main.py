"""
main.py
=======
TradingBot system orchestrator.

Launches the two primary processes:
    1. core.ai_trading_assistant — signal generation every 5 minutes
    2. core.live_trade_tracker   — trade monitoring every 1 minute

Both processes share the same Chrome CDP connection and trade log directory.
Graceful shutdown on Ctrl+C terminates both child processes cleanly.

Usage:
    python main.py

Prerequisites:
    1. Chrome must be running with remote debugging enabled:
       chrome.exe --remote-debugging-port=9222
                  --user-data-dir="C:\\Users\\siddh\\AppData\\Local\\Google\\Chrome\\User Data\\Profile 7"
    2. TradingView must be open in that Chrome window on the NIFTY 5m chart
    3. .env file must be configured (copy from .env.example)
"""

import logging
import subprocess
import sys
import time

from config.config import configure_logging, BASE_DIR

# =========================
# LOGGING
# =========================

configure_logging()
logger = logging.getLogger(__name__)

# =========================
# STARTUP BANNER
# =========================

def print_banner() -> None:
    print("\n" + "=" * 50)
    print("   AI INTRADAY TRADING SYSTEM — PAPER MODE")
    print("=" * 50)
    print(f"   Project root : {BASE_DIR}")
    print("   Mode         : PAPER TRADING ONLY")
    print("   Modules      : ai_trading_assistant + live_trade_tracker")
    print("=" * 50 + "\n")


# =========================
# PROCESS LAUNCHER
# =========================

def launch_process(module: str) -> subprocess.Popen:
    """
    Launch a Python module as a subprocess using the current interpreter.
    Uses python -m <module> from the project BASE_DIR.
    """
    process = subprocess.Popen(
        [sys.executable, "-m", module],
        cwd=str(BASE_DIR),
    )
    logger.info(f"[MAIN] Launched: {module} (PID {process.pid})")
    return process


# =========================
# MAIN
# =========================

def main() -> None:
    print_banner()

    # --- Launch AI Trading Assistant ---
    logger.info("[MAIN] Starting ai_trading_assistant...")
    assistant_process = launch_process("core.ai_trading_assistant")

    # --- Brief delay to allow assistant to write first screenshot ---
    logger.info("[MAIN] Waiting 10s before starting live tracker...")
    time.sleep(10)

    # --- Launch Live Trade Tracker ---
    logger.info("[MAIN] Starting live_trade_tracker...")
    tracker_process = launch_process("core.live_trade_tracker")

    logger.info("[MAIN] Both processes running. Press Ctrl+C to stop.")
    print("\n" + "=" * 50)
    print("   SYSTEM RUNNING — CTRL+C TO STOP")
    print("=" * 50 + "\n")

    # =========================
    # KEEP ALIVE + HEALTH MONITOR
    # =========================

    try:
        while True:
            time.sleep(60)

            # Basic health check — log if a process dies unexpectedly
            if assistant_process.poll() is not None:
                logger.error(
                    f"[MAIN] ⚠️  ai_trading_assistant exited unexpectedly "
                    f"(code: {assistant_process.returncode}). "
                    "Restart main.py to recover."
                )

            if tracker_process.poll() is not None:
                logger.error(
                    f"[MAIN] ⚠️  live_trade_tracker exited unexpectedly "
                    f"(code: {tracker_process.returncode}). "
                    "Restart main.py to recover."
                )

    except KeyboardInterrupt:
        print("\n")
        logger.info("[MAIN] Shutdown signal received (Ctrl+C)")
        logger.info("[MAIN] Terminating ai_trading_assistant...")
        assistant_process.terminate()
        logger.info("[MAIN] Terminating live_trade_tracker...")
        tracker_process.terminate()
        logger.info("[MAIN] System stopped cleanly.")
        print("\n" + "=" * 50)
        print("   SYSTEM STOPPED")
        print("=" * 50 + "\n")


if __name__ == "__main__":
    main()
