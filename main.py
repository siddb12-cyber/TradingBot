"""
main.py
=======
TradingBot — Single entry point.

Architecture (rebuilt 2026-05-27)
-----------------------------------
Single-process, 3-thread engine. No browser. No OCR. No subprocess spawning.

  Thread 1: SignalLoop   — yfinance + NSE API signals every 5 min
  Thread 2: TrackerLoop  — live price monitoring every 30s
  Thread 3: TGPoller     — Telegram inline keyboard callbacks every 3s

All trade approvals go through Telegram inline keyboards (APPROVE / REJECT / SCALE).
Trailing SL activates after each milestone hit.

Usage
-----
  Normal (with terminal, for debugging):
      python main.py

  Hidden background (recommended for daily use):
      Double-click: start.bat

  Stop bot:
      Double-click: stop.bat   OR   Ctrl+C in terminal

Prerequisites
-------------
  1. .env file configured (BOT_TOKEN + CHAT_ID)
  2. pip install -r requirements.txt

Logs
----
  logs/trading.log    — full session log (when started via start.bat)
  decisions/          — per-day signal decision logs (JSON)
  trades/             — per-day trade records (JSON)

PAPER TRADING ONLY — PAPER_TRADING_MODE = True in config/settings.py
"""

import io
import logging
import sys

# =============================================================================
# FORCE UTF-8
# Windows redirects stdout/stderr to cp1252 by default when piped to a file.
# This causes UnicodeEncodeError on any emoji in log output.
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
# STARTUP BANNER
# =============================================================================

def _print_banner() -> None:
    mode = "PAPER TRADING" if PAPER_TRADING_MODE else "LIVE TRADING ⚠"
    print("\n" + "=" * 60)
    print("   AI INTRADAY TRADING SYSTEM  (Rebuilt 2026-05-27)")
    print(f"   Mode     : {mode}")
    print(f"   Root     : {BASE_DIR}")
    print("   Data     : yfinance + NSE API (no browser required)")
    print("   UX       : Telegram inline keyboard approvals")
    print("   Targets  : T1=25 T2=40 T3=60 T4=85 T5=110 ... (+25 each)")
    print("   Trailing : SL → breakeven after T1, trails each milestone")
    print("=" * 60 + "\n")


# =============================================================================
# MAIN
# =============================================================================

def main() -> None:
    _print_banner()

    logger.info("[MAIN] Starting TradingEngine (single-process, 3-thread)")
    logger.info("[MAIN] Mode: %s", "PAPER" if PAPER_TRADING_MODE else "LIVE")
    logger.info("[MAIN] Base directory: %s", BASE_DIR)

    try:
        from core.engine import run
        run()   # Blocks until Ctrl+C or stop.bat
    except KeyboardInterrupt:
        logger.info("[MAIN] Keyboard interrupt — shutting down")
        sys.exit(0)
    except Exception as exc:
        logger.critical("[MAIN] Unhandled startup error: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
