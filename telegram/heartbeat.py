"""
telegram/heartbeat.py
=====================
5-minute heartbeat messages to Telegram.

Purpose
-------
Provides continuous visual confirmation that the bot is alive.
If you don't see a heartbeat in 5 minutes during market hours, the bot has died.

Format (example)
----------------
  💚 BOT ALIVE  09:45  |  OPEN T1_HIT  |  24032.50 +32.5pts

States
------
  Idle          → 💚 BOT ALIVE  09:45  |  No active trade  |  NIFTY 23980.25
  Pending       → ⏳ BOT ALIVE  09:45  |  Waiting approval for BUY 24050 CE
  Open          → 📈 BOT ALIVE  09:45  |  OPEN (T2 hit)  |  24090 +57.5pts
  Market closed → heartbeat sends only once at open and once at close
"""

import logging
from datetime import datetime, date
from typing import Optional, Any

from config.settings import PAPER_TRADING_MODE, MARKET_OPEN_HOUR, MARKET_OPEN_MINUTE

logger = logging.getLogger(__name__)

# How many market-closed heartbeats to skip between sends
_OFFHOURS_SKIP_CYCLES = 12   # Only send every ~60 min off-hours (12 × 5 min)


class Heartbeat:
    """
    Sends 5-minute heartbeat Telegram messages.

    Usage (from engine.py tracker_loop)
    ------------------------------------
        hb = Heartbeat(bot)
        hb.send(trade_record)    # Called every HEARTBEAT_INTERVAL_SECONDS
    """

    def __init__(self, bot: Any) -> None:
        self._bot          = bot
        self._off_cycles   = 0    # Counter for off-hours throttling
        self._last_date    = None # To detect day change
        logger.info("[Heartbeat] Initialised")

    def send(self, trade: Any) -> None:
        """
        Compose and send one heartbeat message.

        Parameters
        ----------
        trade : TradeRecord from TradeManager.get_trade()
        """
        now = datetime.now()

        # ── Off-hours throttling ───────────────────────────────────────────────
        market_open = self._is_market_hours(now)
        if not market_open:
            self._off_cycles += 1
            if self._off_cycles < _OFFHOURS_SKIP_CYCLES:
                return   # Suppress — too frequent
            self._off_cycles = 0

        # ── Compose message ────────────────────────────────────────────────────
        try:
            text = self._compose(trade, now)
            self._bot.send_text(text)
            logger.debug("[Heartbeat] Sent at %s", now.strftime("%H:%M:%S"))
        except Exception as exc:
            logger.warning("[Heartbeat] Failed to send: %s", exc)

    # =========================================================================
    # INTERNAL
    # =========================================================================

    def _compose(self, trade: Any, now: datetime) -> str:
        """Build the heartbeat message string."""
        time_str = now.strftime("%H:%M")
        mode_tag = "📋 PAPER" if PAPER_TRADING_MODE else "🚨 LIVE"

        status = trade.status if trade else "IDLE"

        if status == "IDLE" or not trade or not trade.trade_id:
            # No active trade
            live_price = self._get_live_price_str()
            return (
                f"💚 <b>BOT ALIVE</b>  {time_str}\n"
                f"━━━━━━━━━━━━━━\n"
                f"Mode   : {mode_tag}\n"
                f"Trade  : No active trade\n"
                f"NIFTY  : {live_price}\n"
            )

        elif status == "PENDING":
            return (
                f"⏳ <b>BOT ALIVE</b>  {time_str}\n"
                f"━━━━━━━━━━━━━━\n"
                f"Mode   : {mode_tag}\n"
                f"Trade  : Awaiting approval\n"
                f"Signal : {trade.signal_text}\n"
            )

        elif status == "OPEN":
            entry   = trade.entry_price
            current = trade.current_price
            sl      = trade.sl_price
            pnl_pts = (
                current - entry if trade.direction == "BULLISH"
                else entry - current
            )
            milestone_str = f"T{trade.last_milestone} hit" if trade.last_milestone else "No milestone yet"
            pnl_emoji = "📈" if pnl_pts >= 0 else "📉"

            return (
                f"{pnl_emoji} <b>BOT ALIVE</b>  {time_str}\n"
                f"━━━━━━━━━━━━━━\n"
                f"Mode   : {mode_tag}\n"
                f"Trade  : {trade.signal_text}\n"
                f"Entry  : {entry:.2f}\n"
                f"Now    : {current:.2f}  ({pnl_pts:+.1f}pts)\n"
                f"SL     : {sl:.2f}\n"
                f"Status : {milestone_str}\n"
            )

        else:
            # Some closed state — shouldn't normally be heartbeating with a closed trade
            return (
                f"💚 <b>BOT ALIVE</b>  {time_str}\n"
                f"Mode: {mode_tag} | Status: {status}"
            )

    def _is_market_hours(self, now: datetime) -> bool:
        """True between 09:15 and 15:30 IST."""
        from config.settings import MARKET_OPEN_HOUR, MARKET_OPEN_MINUTE, MARKET_CLOSE_HOUR, MARKET_CLOSE_MINUTE
        open_dt  = now.replace(hour=MARKET_OPEN_HOUR,  minute=MARKET_OPEN_MINUTE,  second=0)
        close_dt = now.replace(hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MINUTE, second=0)
        return open_dt <= now < close_dt

    def _get_live_price_str(self) -> str:
        """Attempt to get a live NIFTY price string for heartbeat (best-effort)."""
        try:
            from core.data_engine import DataEngine
            de = DataEngine()
            data = de.get_live_price()
            price = data.get("price")
            if price:
                return f"{price:.2f}"
        except Exception:
            pass
        return "N/A"
