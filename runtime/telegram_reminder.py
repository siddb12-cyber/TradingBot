"""
runtime/telegram_reminder.py
============================
Daily 8:30 AM IST Telegram reminder for the TradingBot user.

Purpose
-------
Sent each weekday morning, 45 minutes before NIFTY market open (09:15 IST),
to remind the operator to:
    - Boot the TradingBot system
    - Open TradingView with the NIFTY chart
    - Review pre-market data (SGX, US close, VIX)
    - Confirm Telegram bot is online

Invocation
----------
    cd C:\\Users\\siddh\\Downloads\\HK\\TradingBot
    python -m runtime.telegram_reminder

Designed to be triggered by a Cowork scheduled task. Runs silently and
exits 0 on success, non-zero on failure.

PAPER TRADING ONLY — this script sends a notification, never trades.
"""

# =============================================================================
# IMPORTS
# =============================================================================

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure project root is on sys.path when invoked as `python -m runtime.telegram_reminder`
_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config.settings import BOT_TOKEN, CHAT_ID, PAPER_TRADING_MODE  # noqa: E402
from telegram.bot import TelegramBot                                # noqa: E402


# =============================================================================
# LOGGING
# =============================================================================

# Configure a lightweight stream logger for the reminder script.
# We do NOT depend on the main app logger, so this stays runnable standalone.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("telegram_reminder")


# =============================================================================
# CONSTANTS
# =============================================================================

# India Standard Time = UTC+5:30 (no DST). Use a fixed offset rather than
# pulling in pytz/zoneinfo as a dependency — keeps script lean.
IST = timezone(timedelta(hours=5, minutes=30))

# Market timing reference (IST)
MARKET_OPEN_TIME  = "09:15"
MARKET_CLOSE_TIME = "15:30"

# Days when NSE is closed (weekends; manual holidays can be added below)
NSE_HOLIDAYS_2026: set[str] = {
    # Add NSE 2026 trading holidays here in "YYYY-MM-DD" format if needed.
    # Source: https://www.nseindia.com/resources/exchange-communication-holidays
}


# =============================================================================
# HELPERS
# =============================================================================

def _ist_now() -> datetime:
    """Return the current time in India Standard Time."""
    return datetime.now(IST)


def _is_market_day(dt: datetime) -> bool:
    """True if `dt` falls on a weekday and is not a known NSE holiday."""
    if dt.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
        return False
    if dt.strftime("%Y-%m-%d") in NSE_HOLIDAYS_2026:
        return False
    return True


def _build_message(now_ist: datetime, market_day: bool) -> str:
    """
    Build the morning reminder text.

    Two variants:
        - Market day  → full pre-market briefing reminder
        - Off day     → short note acknowledging the market is closed
    """
    date_str = now_ist.strftime("%A, %d %B %Y")
    time_str = now_ist.strftime("%H:%M IST")
    mode_str = "📋 PAPER TRADING" if PAPER_TRADING_MODE else "🚨 LIVE TRADING"

    # ── Off-day variant ───────────────────────────────────────────────────────
    if not market_day:
        return (
            f"☕ <b>Good Morning, Sid</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Date : {date_str}\n"
            f"Time : {time_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📭 <b>Market Closed Today</b>\n"
            f"NSE is not open today. No trading session.\n"
            f"Use the day to review last week's analytics and refine the strategy.\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"{mode_str}"
        )

    # ── Market-day variant ────────────────────────────────────────────────────
    return (
        f"☕ <b>Good Morning, Sid</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Date   : {date_str}\n"
        f"Time   : {time_str}\n"
        f"Opens  : {MARKET_OPEN_TIME} IST  (in ~45 min)\n"
        f"Closes : {MARKET_CLOSE_TIME} IST\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔔 <b>Pre-Market Checklist</b>\n"
        f"  1. Start TradingBot   →  <code>start.bat</code>\n"
        f"  2. Open TradingView   →  NIFTY 5m + VWAP + EMA9\n"
        f"  3. Check SGX NIFTY    →  bias for the open\n"
        f"  4. Check US close     →  Dow / Nasdaq / S&amp;P\n"
        f"  5. Check India VIX    →  volatility regime\n"
        f"  6. Confirm bot online →  reply /ping in this chat\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 <b>Strategy Reminder</b>\n"
        f"  • Bullish : price &gt; VWAP &amp; price &gt; EMA9  → ATM CE\n"
        f"  • Bearish : price &lt; VWAP &amp; price &lt; EMA9  → ATM PE\n"
        f"  • Sideways: no trade\n"
        f"  • SL: 10 pts | T1: +25 | T2: +40 | T3: +60\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{mode_str} — no real money at risk."
    )


# =============================================================================
# MAIN
# =============================================================================

def main() -> int:
    """
    Build and send the morning reminder. Returns a Unix-style exit code:
        0 → success
        1 → configuration missing
        2 → Telegram send failed
    """
    logger.info("Telegram reminder script started")

    # ── Validate config ───────────────────────────────────────────────────────
    if not BOT_TOKEN or not CHAT_ID:
        logger.error(
            "BOT_TOKEN / CHAT_ID missing from environment. "
            "Check .env at project root."
        )
        return 1

    # ── Build payload ─────────────────────────────────────────────────────────
    now_ist    = _ist_now()
    market_day = _is_market_day(now_ist)
    text       = _build_message(now_ist, market_day)

    logger.info(
        "Built reminder | ist=%s | market_day=%s | paper=%s",
        now_ist.strftime("%Y-%m-%d %H:%M"),
        market_day,
        PAPER_TRADING_MODE,
    )

    # ── Dispatch ──────────────────────────────────────────────────────────────
    bot     = TelegramBot()
    msg_id  = bot.send_text(text)

    if msg_id is None:
        logger.error("Telegram send failed — see TGBot logs above.")
        return 2

    logger.info("Reminder sent successfully | telegram_msg_id=%s", msg_id)
    return 0


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    sys.exit(main())
