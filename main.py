"""
main.py
=======
TradingBot — single entry point.

Architecture (new, as of 2026-05-20)
-------------------------------------
Single-process, 3-thread engine — no browser, no OCR, no subprocess spawning.

  Thread 1: signal_loop     — yfinance + NSE API signals every 5 min
  Thread 2: tracker_loop    — live price monitoring every 60s
  Thread 3: telegram_poller — inline keyboard callbacks every 3s

All trade approvals go through Telegram inline keyboards (APPROVE/REJECT/SCALE).

Usage
-----
  Normal (with terminal):
      python main.py

  Hidden (no terminal window) — RECOMMENDED:
      Double-click: start_hidden.vbs

  Stop bot:
      Double-click: stop_bot.vbs
      OR: Ctrl+C in terminal

Prerequisites
-------------
  1. .env file configured (copy .env.example → .env, fill BOT_TOKEN + CHAT_ID)
  2. pip install -r requirements.txt  (includes yfinance, pandas)
  3. Chrome NOT required — all data comes from yfinance + NSE API

Logs
----
  logs/trading.log   — full session log (when started via start_hidden.vbs)
  trade_logs/        — per-day Excel decision journals + trade records

Previous architecture (deprecated)
-----------------------------------
The old main.py launched two subprocesses:
  - core.ai_trading_assistant  (screenshots + OCR + Playwright)
  - core.live_trade_tracker    (monitoring loop)

This caused crashes because:
  - Chrome + TradingView had to be running 24/7
  - Two subprocesses sharing state via JSON file
  - Any browser crash killed the entire system

The new trading_engine.py replaces both with a robust single-process design.
"""

import logging
import sys

from config.config import configure_logging, BASE_DIR, PAPER_TRADING_MODE

# =========================
# LOGGING
# =========================

configure_logging()
logger = logging.getLogger(__name__)


# =========================
# STARTUP BANNER
# =========================

def _print_banner() -> None:
    mode = "📄 PAPER TRADING" if PAPER_TRADING_MODE else "⚠️  LIVE TRADING"
    print("\n" + "=" * 58)
    print("   AI INTRADAY TRADING SYSTEM")
    print(f"   Mode   : {mode}")
    print(f"   Root   : {BASE_DIR}")
    print("   Data   : yfinance + NSE API (no browser required)")
    print("   UX     : Telegram inline keyboard approvals")
    print("=" * 58 + "\n")


# =========================
# MAIN
# =========================

def main() -> None:
    _print_banner()

    logger.info("[MAIN] Starting TradingEngine (single-process, 3-thread)")
    logger.info("[MAIN] Mode: %s", "PAPER" if PAPER_TRADING_MODE else "LIVE")
    logger.info("[MAIN] Base directory: %s", BASE_DIR)

    # ---- Import and run the new unified engine ----
    try:
        from core.trading_engine import run
        run()   # Blocks until stopped (Ctrl+C or stop_bot.vbs)
    except KeyboardInterrupt:
        logger.info("[MAIN] Keyboard interrupt — shutting down")
        sys.exit(0)
    except Exception as exc:
        logger.critical("[MAIN] Unhandled startup error: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
