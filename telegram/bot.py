"""
telegram/bot.py
===============
All Telegram API communication for TradingBot.

Replaces core/telegram_approval_bot.py.

Features
--------
- Retry logic with exponential backoff on all API calls
- Inline keyboard for APPROVE / REJECT / SCALE
- Formatted messages for: startup, shutdown, signal request, trade open,
  target hits (T1–T10), trade close (SL / reversal / EOD)
- DNS failure handling (demoted to DEBUG for transient blips)

Paper Trading Only — no execution logic here.
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional

import requests

from config.settings import BOT_TOKEN, CHAT_ID, PAPER_TRADING_MODE

logger = logging.getLogger(__name__)

# =============================================================================
# CONSTANTS
# =============================================================================

_TG_BASE   = f"https://api.telegram.org/bot{BOT_TOKEN}"
_POST_RETRY = 3      # Max retries for POST (sendMessage etc.)
_GET_RETRY  = 2      # Max retries for GET (getUpdates)
_RETRY_WAIT = 2      # Seconds between retries

# Emoji shortcuts
_BULL  = "🟢"
_BEAR  = "🔴"
_TGT   = "🎯"
_WARN  = "⚠️"
_CHECK = "✅"
_CLOCK = "🕐"
_HEART = "💚"
_PAPER = "📋"
_FIRE  = "🔥"


# =============================================================================
# LOW-LEVEL API HELPERS
# =============================================================================

def _tg_post(method: str, payload: Dict, retries: int = _POST_RETRY) -> Optional[Dict]:
    """POST to Telegram API with retry + backoff."""
    url = f"{_TG_BASE}/{method}"
    for attempt in range(retries):
        try:
            resp = requests.post(url, json=payload, timeout=10)
            data = resp.json()
            if data.get("ok"):
                return data
            logger.warning("[TGBot] API error on %s: %s", method, data.get("description"))
            return data
        except requests.exceptions.ConnectionError as exc:
            log = logger.debug if attempt < retries - 1 else logger.warning
            log("[TGBot] Connection error on %s (attempt %d): %s", method, attempt + 1, exc)
        except Exception as exc:
            logger.warning("[TGBot] Unexpected error on %s: %s", method, exc)
        if attempt < retries - 1:
            time.sleep(_RETRY_WAIT * (2 ** attempt))
    return None


def _tg_get(method: str, params: Dict = None, retries: int = _GET_RETRY) -> Optional[Dict]:
    """GET from Telegram API with retry + backoff."""
    url = f"{_TG_BASE}/{method}"
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params or {}, timeout=10)
            data = resp.json()
            if data.get("ok"):
                return data
            return data
        except requests.exceptions.ConnectionError as exc:
            logger.debug("[TGBot] DNS/conn error on %s (attempt %d): %s", method, attempt + 1, exc)
        except Exception as exc:
            logger.debug("[TGBot] GET error on %s: %s", method, exc)
        if attempt < retries - 1:
            time.sleep(_RETRY_WAIT)
    return None


# =============================================================================
# TELEGRAM BOT CLASS
# =============================================================================

class TelegramBot:
    """
    High-level Telegram interface for TradingBot.

    All message formats are defined in this class.
    engine.py calls these methods — it never touches the Telegram API directly.
    """

    def __init__(self) -> None:
        logger.info("[TGBot] Initialised | chat_id=%s paper=%s", CHAT_ID, PAPER_TRADING_MODE)

    # =========================================================================
    # SIMPLE TEXT
    # =========================================================================

    def send_text(self, text: str, parse_mode: str = "HTML") -> Optional[int]:
        """
        Send a plain text message.
        Returns the Telegram message_id or None on failure.
        """
        payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": parse_mode}
        resp = _tg_post("sendMessage", payload)
        if resp and resp.get("ok"):
            return resp["result"]["message_id"]
        return None

    # =========================================================================
    # LIFECYCLE MESSAGES
    # =========================================================================

    def send_startup(self) -> None:
        """Bot startup notification."""
        from datetime import datetime
        mode = f"{_PAPER} PAPER TRADING" if PAPER_TRADING_MODE else "🚨 LIVE TRADING"
        text = (
            f"{_HEART} <b>TradingBot Started</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Mode    : {mode}\n"
            f"Time    : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Data    : yfinance + NSE API\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"System is running. Waiting for signals..."
        )
        self.send_text(text)
        logger.info("[TGBot] Startup message sent")

    def send_shutdown(self) -> None:
        """Bot shutdown notification."""
        from datetime import datetime
        text = (
            f"🔴 <b>TradingBot Stopped</b>\n"
            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        self.send_text(text)
        logger.info("[TGBot] Shutdown message sent")

    # =========================================================================
    # SIGNAL REQUEST (with inline keyboard)
    # =========================================================================

    def send_signal_request(self, signal: Dict, lots: int) -> Optional[int]:
        """
        Send trade approval request with inline keyboard.

        Returns the Telegram message_id (stored in TradeRecord.tg_message_id).
        """
        direction   = signal.get("direction", "")
        trade_sig   = signal.get("trade_signal", "")
        score       = signal.get("adjusted_score", 0)
        conf        = signal.get("confidence_level", "")
        price       = signal.get("price") or 0.0
        vwap        = signal.get("vwap")
        ema9        = signal.get("ema9")
        align       = signal.get("alignment_summary", "N/A")

        dir_emoji = _BULL if direction == "BULLISH" else _BEAR

        from config.settings import (
            STOP_LOSS_POINTS, TARGET_1_POINTS, TARGET_2_POINTS, TARGET_3_POINTS,
            NIFTY_LOT_SIZE, OPTION_DELTA, ACCOUNT_CAPITAL, get_target_points
        )

        # INR risk/reward estimates
        pts_per_lot = NIFTY_LOT_SIZE * OPTION_DELTA
        risk_inr    = STOP_LOSS_POINTS * pts_per_lot * lots
        t1_inr      = TARGET_1_POINTS  * pts_per_lot * lots
        t2_inr      = TARGET_2_POINTS  * pts_per_lot * lots
        t3_inr      = TARGET_3_POINTS  * pts_per_lot * lots

        vwap_str = f"{vwap:.2f}" if vwap else "N/A"
        ema9_str = f"{ema9:.2f}" if ema9 else "N/A"

        text = (
            f"{dir_emoji} <b>SIGNAL — {trade_sig}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Direction   : {direction}\n"
            f"Price       : {price:.2f}\n"
            f"VWAP        : {vwap_str}\n"
            f"EMA9        : {ema9_str}\n"
            f"Alignment   : {align}\n"
            f"Confidence  : <b>{conf}</b> ({score}/100)\n"
            f"Lots        : {lots}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"SL          : -{STOP_LOSS_POINTS}pts  ≈ -₹{risk_inr:.0f}\n"
            f"T1 (exit ⅓) : +{TARGET_1_POINTS}pts  ≈ +₹{t1_inr:.0f}\n"
            f"T2 (exit ⅓) : +{TARGET_2_POINTS}pts  ≈ +₹{t2_inr:.0f}\n"
            f"T3 (exit ⅓) : +{TARGET_3_POINTS}pts  ≈ +₹{t3_inr:.0f}\n"
            f"T4+         : Trailing until reversal\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"{_PAPER} PAPER TRADE — no real money at risk\n"
            f"Approve to simulate entry:"
        )

        # Inline keyboard — APPROVE / SCALE / REJECT
        keyboard = {
            "inline_keyboard": [[
                {"text": f"{_CHECK} APPROVE",  "callback_data": "APPROVE"},
                {"text": f"{_FIRE} SCALE 2x",  "callback_data": "SCALE"},
                {"text": "❌ REJECT",           "callback_data": "REJECT"},
            ]]
        }

        payload = {
            "chat_id":      CHAT_ID,
            "text":         text,
            "parse_mode":   "HTML",
            "reply_markup": keyboard,
        }
        resp = _tg_post("sendMessage", payload)
        if resp and resp.get("ok"):
            msg_id = resp["result"]["message_id"]
            logger.info("[TGBot] Signal request sent | msg_id=%d", msg_id)
            return msg_id
        logger.error("[TGBot] Failed to send signal request")
        return None

    # =========================================================================
    # TRADE OPEN CONFIRMATION
    # =========================================================================

    def send_trade_open(self, trade: Any) -> None:
        """Send confirmation when trade transitions from PENDING → OPEN."""
        dir_emoji = _BULL if trade.direction == "BULLISH" else _BEAR
        text = (
            f"{dir_emoji} <b>TRADE OPENED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"ID          : {trade.trade_id}\n"
            f"Signal      : {trade.signal_text}\n"
            f"Entry       : {trade.entry_price:.2f}\n"
            f"Initial SL  : {trade.initial_sl:.2f}  (-{trade.entry_price - trade.initial_sl:.0f}pts)\n"
            f"Lots        : {trade.lots}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Watching for T1=+25 | T2=+40 | T3=+60\n"
            f"Trailing SL activates after T1\n"
            f"{_PAPER} PAPER TRADE"
        )
        self.send_text(text)
        logger.info("[TGBot] Trade open message sent | id=%s", trade.trade_id)

    # =========================================================================
    # TARGET HIT NOTIFICATIONS
    # =========================================================================

    def send_target_hit(self, trade: Any, n: int, price: float) -> None:
        """Send notification when target Tn is hit — with full booking breakdown."""
        from config.settings import (
            get_target_points, NIFTY_LOT_SIZE, OPTION_DELTA,
            SL_AFTER_T1_OFFSET, BOOKING_FRACTION,
        )

        pts         = get_target_points(n)
        pts_per_lot = NIFTY_LOT_SIZE * OPTION_DELTA

        # ── New SL level ──────────────────────────────────────────────────────
        if n == 1:
            new_sl_offset = SL_AFTER_T1_OFFSET
            sl_note       = f"BE+{SL_AFTER_T1_OFFSET}pts (min profit locked)"
        else:
            new_sl_offset = get_target_points(n - 1)
            sl_note       = f"T{n-1} level (+{new_sl_offset}pts)"

        new_sl = (
            trade.entry_price + new_sl_offset if trade.direction == "BULLISH"
            else trade.entry_price - new_sl_offset
        )

        # ── Booking breakdown ─────────────────────────────────────────────────
        booking_lines = []
        cumulative_inr = 0.0
        for i in range(1, n + 1):
            bk = (trade.milestone_bookings or {}).get(f"T{i}", {})
            b_pts  = bk.get("pts", get_target_points(i))
            b_lots = bk.get("lots_booked", 0.0)
            b_inr  = bk.get("inr", 0.0)
            b_prc  = bk.get("price", 0.0)
            if b_lots > 0:
                cumulative_inr += b_inr
                booking_lines.append(
                    f"  T{i} @ {b_prc:.2f}  +{b_pts}pts  "
                    f"{b_lots:.2f} lots  ₹{b_inr:,.0f}"
                )

        if booking_lines:
            booking_block = "\n".join(booking_lines)
            booking_section = (
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"📊 <b>Booking Breakdown (⅓ per target)</b>\n"
                f"{booking_block}\n"
                f"  Cumulative booked  : ₹{cumulative_inr:,.0f}\n"
            )
        else:
            total_inr = pts * pts_per_lot * trade.lots
            booking_section = (
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"📊 Virtual T{n} — full position running\n"
                f"  P&amp;L if closed now : ₹{total_inr:,.0f}\n"
            )

        # Running remainder info
        booked_fractions = sum(
            1 for i in range(1, n + 1)
            if (trade.milestone_bookings or {}).get(f"T{i}", {}).get("lots_booked", 0) > 0
        )
        booked_pct   = min(booked_fractions * 33, 100)
        running_pct  = 100 - booked_pct
        running_lots = round(trade.lots * running_pct / 100, 2)

        text = (
            f"{_TGT} <b>TARGET T{n} HIT!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Signal      : {trade.signal_text}\n"
            f"Entry       : {trade.entry_price:.2f}\n"
            f"T{n} Price  : {price:.2f}  (+{pts}pts)\n"
            f"Lots        : {trade.lots}\n"
            f"{booking_section}"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🛡️ <b>SL Updated</b>\n"
            f"  {trade.sl_price:.2f} → <b>{new_sl:.2f}</b>  ({sl_note})\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 Running {running_lots} lots for T{n+1}+ 🚀"
        )
        self.send_text(text)
        logger.info("[TGBot] T%d hit message sent | booked_cumulative=₹%.0f", n, cumulative_inr)

    # =========================================================================
    # TRADE CLOSE
    # =========================================================================

    def send_trade_close(self, trade: Any) -> None:
        """Send trade close summary."""
        from datetime import datetime

        pnl_pts = trade.pnl_points
        pnl_inr = trade.pnl_inr
        reason  = trade.close_reason

        # Select emoji based on outcome
        if pnl_pts > 0:
            result_emoji = "✅"
            result_label = "PROFIT"
        elif pnl_pts < 0:
            result_emoji = "🔴"
            result_label = "LOSS"
        else:
            result_emoji = "⚪"
            result_label = "BREAKEVEN"

        milestones_str = (
            ", ".join(f"T{n}" for n in sorted(trade.milestones_hit))
            if trade.milestones_hit else "None"
        )

        # Per-milestone booking breakdown for close summary
        booking_lines = []
        total_booked_inr = 0.0
        for key, bk in sorted((trade.milestone_bookings or {}).items()):
            b_inr  = bk.get("inr", 0.0)
            b_lots = bk.get("lots_booked", 0.0)
            b_pts  = bk.get("pts", 0)
            b_prc  = bk.get("price", 0.0)
            if b_lots > 0:
                total_booked_inr += b_inr
                booking_lines.append(
                    f"  {key}: {b_lots:.2f} lots @ +{b_pts}pts @ {b_prc:.2f} = ₹{b_inr:,.0f}"
                )
        booking_detail = ("\n" + "\n".join(booking_lines)) if booking_lines else "  None"

        # Duration
        try:
            open_dt  = datetime.fromisoformat(trade.open_time)
            close_dt = datetime.fromisoformat(trade.close_time)
            mins     = int((close_dt - open_dt).total_seconds() // 60)
            duration = f"{mins}m"
        except Exception:
            duration = "N/A"

        text = (
            f"{result_emoji} <b>TRADE CLOSED — {result_label}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"ID          : {trade.trade_id}\n"
            f"Signal      : {trade.signal_text}\n"
            f"Entry       : {trade.entry_price:.2f}\n"
            f"Close       : {trade.current_price:.2f}\n"
            f"Reason      : {reason}\n"
            f"Duration    : {duration}\n"
            f"Lots        : {trade.lots}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"P&amp;L (total) : <b>{pnl_pts:+.1f} pts  ≈ ₹{pnl_inr:+.0f}</b>\n"
            f"Milestones  : {milestones_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 <b>Booking Breakdown</b>{booking_detail}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"{_PAPER} PAPER TRADE"
        )
        self.send_text(text)
        logger.info(
            "[TGBot] Trade close sent | id=%s pnl=%.1fpts ₹%.0f",
            trade.trade_id, pnl_pts, pnl_inr,
        )

    # =========================================================================
    # CALLBACK ANSWER
    # =========================================================================

    def answer_callback(self, callback_query_id: str, text: str = "") -> None:
        """Answer a callback query to remove the loading spinner on Telegram."""
        _tg_post("answerCallbackQuery", {
            "callback_query_id": callback_query_id,
            "text": text,
        })

    # =========================================================================
    # GET UPDATES
    # =========================================================================

    def get_updates(self, offset: Optional[int] = None) -> List[Dict]:
        """
        Poll Telegram for new updates.
        Returns list of update dicts (may be empty).
        """
        params = {
            "timeout":         1,
            "allowed_updates": ["callback_query"],
        }
        if offset is not None:
            params["offset"] = offset

        resp = _tg_get("getUpdates", params)
        if resp and resp.get("ok"):
            return resp.get("result", [])
        return []
