"""
main.py
=======
TradingBot — Single entry point.

Architecture
-----------------------------------
Single-process, multi-thread engine + system tray app.

  Thread 1: SignalLoop   — yfinance + NSE API signals every 5 min
  Thread 2: TrackerLoop  — live price monitoring every 30s
  Thread 3: TGPoller     — Telegram inline keyboard callbacks every 3s
  Thread 4: Dashboard    — Flask on http://localhost:5050
  Thread 5: TrayIcon     — pystray Windows system tray (optional)

Usage
-----
  Double-click start.bat   — recommended (background, auto-restart)
  python main.py           — foreground with system tray + terminal output

Stop
----
  Right-click tray icon → Stop Bot
  OR double-click stop.bat
  OR Ctrl+C in terminal

Prerequisites
-------------
  1. .env file configured (BOT_TOKEN + CHAT_ID)
  2. pip install -r requirements.txt

Logs
----
  logs/trading.log    — full session log
  decisions/          — per-day signal decision logs (JSON)
  trades/             — per-day trade records (JSON)

PAPER TRADING ONLY — PAPER_TRADING_MODE = True in config/settings.py
"""

import io
import logging
import os
import sys
import threading

# =============================================================================
# FORCE UTF-8
# =============================================================================
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from config.settings import configure_logging, BASE_DIR, PAPER_TRADING_MODE

# =============================================================================
# LOGGING
# =============================================================================

configure_logging()
logger = logging.getLogger(__name__)

# =============================================================================
# DASHBOARD PORT
# =============================================================================

DASHBOARD_PORT: int = int(os.getenv("DASHBOARD_PORT", "5050"))


# =============================================================================
# STARTUP BANNER
# =============================================================================

def _print_banner() -> None:
    mode = "PAPER TRADING" if PAPER_TRADING_MODE else "LIVE TRADING"
    print("\n" + "=" * 60)
    print("   AI INTRADAY TRADING SYSTEM")
    print(f"   Mode     : {mode}")
    print(f"   Root     : {BASE_DIR}")
    print("   Data     : yfinance + NSE API")
    print("   UX       : Telegram inline keyboard + Windows tray")
    print("   Targets  : T1=25 T2=40 T3=60 T4=85 T5=110 ... (+25)")
    print("   Trailing : BE+15 after T1, trails T(n-1) each milestone")
    print(f"   Dashboard: http://localhost:{DASHBOARD_PORT}")
    print("=" * 60 + "\n")


# =============================================================================
# DASHBOARD — background daemon thread
# =============================================================================

def _start_dashboard() -> None:
    """Start Flask dashboard in a daemon thread."""
    try:
        import logging as _log
        _log.getLogger("werkzeug").setLevel(_log.WARNING)
        from dashboard.server import app
        logger.info("[MAIN] Dashboard starting on http://localhost:%d", DASHBOARD_PORT)
        app.run(
            host="0.0.0.0",
            port=DASHBOARD_PORT,
            debug=False,
            use_reloader=False,
        )
    except Exception as exc:
        logger.warning("[MAIN] Dashboard failed to start: %s", exc)


# =============================================================================
# SYSTEM TRAY — background daemon thread
# =============================================================================

def _start_tray() -> None:
    """Start Windows system tray icon (requires pystray + Pillow)."""
    try:
        from app import tray
        tray.start()
        logger.info("[MAIN] System tray started")
    except Exception as exc:
        logger.warning("[MAIN] Tray failed to start: %s — tray disabled", exc)


# =============================================================================
# MAIN
# =============================================================================

def main() -> None:
    _print_banner()

    logger.info("[MAIN] Starting TradingBot (multi-thread)")
    logger.info("[MAIN] Mode: %s", "PAPER" if PAPER_TRADING_MODE else "LIVE")
    logger.info("[MAIN] Base directory: %s", BASE_DIR)

    # ── Start dashboard ───────────────────────────────────────────────────────
    dash_thread = threading.Thread(
        target=_start_dashboard, name="Dashboard", daemon=True
    )
    dash_thread.start()

    # ── Start system tray icon ────────────────────────────────────────────────
    _start_tray()

    # ── Start trading engine (blocks until stopped) ───────────────────────────
    try:
        from core.engine import run
        run()
    except KeyboardInterrupt:
        logger.info("[MAIN] Keyboard interrupt — shutting down")
        sys.exit(0)
    except Exception as exc:
        logger.critical("[MAIN] Unhandled startup error: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
