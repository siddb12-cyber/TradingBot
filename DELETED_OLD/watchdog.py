"""
watchdog.py
===========
Auto-restart wrapper for TradingBot main.py.

Keeps the bot alive permanently — if main.py crashes or exits for any reason,
this script waits 15 seconds and restarts it automatically.

Usage
-----
  Visible terminal (for debugging):
      python watchdog.py

  Hidden (recommended for daily use):
      Double-click: start_hidden.vbs  (points to watchdog.py, not main.py)

Logs
----
  Every restart is logged to logs/watchdog.log with timestamp and exit code.

Stop
----
  Double-click: stop_bot.vbs
  OR: Ctrl+C in terminal (stops both watchdog and bot)
"""

import subprocess
import sys
import time
import os
from datetime import datetime
from pathlib import Path

# ── Config ─────────────────────────────────────────────────────────────────
BASE_DIR      = Path(__file__).parent.resolve()
MAIN_SCRIPT   = BASE_DIR / "main.py"
LOG_FILE      = BASE_DIR / "logs" / "watchdog.log"
RESTART_DELAY = 15          # seconds between crash and restart
MAX_RESTARTS  = 50          # safety cap — stops after 50 restarts in a session

# ── Logging helper ──────────────────────────────────────────────────────────
def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── Main watchdog loop ──────────────────────────────────────────────────────
def main() -> None:
    log("=" * 60)
    log("TradingBot Watchdog started")
    log(f"  Script : {MAIN_SCRIPT}")
    log(f"  Python : {sys.executable}")
    log(f"  Restart delay : {RESTART_DELAY}s | Max restarts: {MAX_RESTARTS}")
    log("=" * 60)

    restart_count = 0

    while restart_count < MAX_RESTARTS:
        log(f"[Watchdog] Starting bot (attempt #{restart_count + 1})...")

        try:
            proc = subprocess.run(
                [sys.executable, str(MAIN_SCRIPT)],
                cwd=str(BASE_DIR),
            )
            exit_code = proc.returncode
        except KeyboardInterrupt:
            log("[Watchdog] Ctrl+C received — stopping watchdog and bot.")
            sys.exit(0)
        except Exception as exc:
            log(f"[Watchdog] Failed to launch bot: {exc}")
            exit_code = -1

        restart_count += 1

        # Clean exit (exit code 0) = intentional stop (stop_bot.vbs or Ctrl+C)
        if exit_code == 0:
            log("[Watchdog] Bot exited cleanly (code 0) — not restarting.")
            break

        log(
            f"[Watchdog] Bot exited with code {exit_code}. "
            f"Restart #{restart_count} in {RESTART_DELAY}s..."
        )

        # Wait before restarting, but allow Ctrl+C to interrupt
        try:
            time.sleep(RESTART_DELAY)
        except KeyboardInterrupt:
            log("[Watchdog] Ctrl+C during wait — stopping.")
            sys.exit(0)

    if restart_count >= MAX_RESTARTS:
        log(f"[Watchdog] ⚠️  Hit max restarts ({MAX_RESTARTS}). Stopping watchdog.")
        log("[Watchdog] Check logs/trading.log for the root cause.")


if __name__ == "__main__":
    main()
