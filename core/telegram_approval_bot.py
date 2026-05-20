"""
core/telegram_approval_bot.py
==============================
Telegram inline keyboard bot for all trade approval flows.

Replaces the text-based telegram_approval.py with a modern inline keyboard UX.
Every trade decision — entry, scale-up, SL modification, target change, close —
goes through this module for your one-tap approval on Telegram.

How it works
------------
1. trading_engine.py calls send_signal_request() when a trade signal is ready.
2. This module sends a rich Telegram message with inline keyboard buttons:
       [✅ APPROVE]  [❌ REJECT]  [📈 SCALE x2]
3. The Telegram poller thread (running in trading_engine.py) calls
   process_callback(update) when a callback_query arrives.
4. Callbacks are matched to pending requests by trade_id.
5. The matched request resolves its threading.Event, unblocking the caller.

Inline keyboard design
-----------------------
Signal approval:
    Row 1: [✅ APPROVE]   [❌ REJECT]
    Row 2: [📈 SCALE x2]  (only when confidence = VERY HIGH)

Active trade management:
    Row 1: [🔒 Tighten SL]  [📈 Trail SL]
    Row 2: [🎯 Move T2→T1]  [❌ Close Now]

Callback format:  "<action>:<trade_id>"
    e.g. "approve:PAPER-20260520-001"
         "reject:PAPER-20260520-001"
         "scale:PAPER-20260520-001"
         "tighten_sl:PAPER-20260520-001"
         "trail_sl:PAPER-20260520-001"
         "close:PAPER-20260520-001"

Thread safety
-------------
_pending_requests dict is protected by a threading.Lock.
Each pending request uses its own threading.Event for blocking wait.
A background expiry thread removes stale requests.

Auto-expiry
-----------
Requests expire after TELEGRAM_APPROVAL_TIMEOUT_MINUTES.
On expiry: callback sends a "⏱ Request expired" edit to the original message
and the waiting caller receives outcome=EXPIRED.
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, Optional

import requests

from config.config import (
    BOT_TOKEN,
    CHAT_ID,
    TELEGRAM_APPROVAL_TIMEOUT_MINUTES,
    STOP_LOSS_POINTS,
    TARGET_1_POINTS,
    TARGET_2_POINTS,
    TARGET_3_POINTS,
)

# =========================
# MODULE LOGGER
# =========================

logger = logging.getLogger(__name__)

# =========================
# TELEGRAM BASE URL
# =========================

_TG_BASE   = f"https://api.telegram.org/bot{BOT_TOKEN}"
_TIMEOUT_S = 10   # HTTP request timeout (seconds)


# =========================
# OUTCOME ENUM
# =========================

class CallbackOutcome(Enum):
    """Possible outcomes of a Telegram inline keyboard approval."""
    APPROVED   = "APPROVED"
    REJECTED   = "REJECTED"
    SCALED     = "SCALED"       # User tapped SCALE x2
    TIGHTEN_SL = "TIGHTEN_SL"  # Tighten stop loss
    TRAIL_SL   = "TRAIL_SL"    # Trail stop loss up
    CLOSE_NOW  = "CLOSE_NOW"   # Force close trade
    EXPIRED    = "EXPIRED"      # Timeout — no reply


# =========================
# PENDING REQUEST RECORD
# =========================

@dataclass
class _PendingRequest:
    """Tracks one outstanding Telegram approval request."""
    trade_id:    str
    message_id:  int                            # Telegram message_id for later editing
    expires_at:  float                          # Unix timestamp when request expires
    event:       threading.Event = field(default_factory=threading.Event)
    outcome:     Optional[CallbackOutcome] = None
    action_data: Dict = field(default_factory=dict)  # e.g. {"lots": 2}


# =========================
# TELEGRAM HTTP HELPERS
# =========================

def _tg_post(method: str, payload: dict) -> Optional[dict]:
    """POST to Telegram Bot API. Returns response JSON or None on error."""
    url = f"{_TG_BASE}/{method}"
    try:
        resp = requests.post(url, json=payload, timeout=_TIMEOUT_S)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.error("[TGBot] API call %s failed: %s", method, exc)
        return None


def _tg_get(method: str, params: dict) -> Optional[dict]:
    """GET from Telegram Bot API. Returns response JSON or None on error."""
    url = f"{_TG_BASE}/{method}"
    try:
        resp = requests.get(url, params=params, timeout=_TIMEOUT_S)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.error("[TGBot] API call %s failed: %s", method, exc)
        return None


def _send_message(text: str, reply_markup: Optional[dict] = None) -> Optional[int]:
    """
    Send a Telegram message to CHAT_ID.
    Returns message_id on success, None on failure.
    """
    payload: dict = {
        "chat_id":    CHAT_ID,
        "text":       text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    resp = _tg_post("sendMessage", payload)
    if resp and resp.get("ok"):
        return resp["result"]["message_id"]
    return None


def _edit_message(message_id: int, text: str) -> None:
    """Edit the text of a previously sent Telegram message (remove inline keyboard)."""
    payload = {
        "chat_id":      CHAT_ID,
        "message_id":   message_id,
        "text":         text,
        "parse_mode":   "HTML",
        "reply_markup": {"inline_keyboard": []},  # Clears buttons
    }
    _tg_post("editMessageText", payload)


def answer_callback_query(callback_query_id: str, text: str = "") -> None:
    """Acknowledge a callback query to dismiss the loading spinner on the button."""
    _tg_post("answerCallbackQuery", {
        "callback_query_id": callback_query_id,
        "text":              text,
        "show_alert":        False,
    })


# =========================
# INLINE KEYBOARD BUILDERS
# =========================

def _signal_keyboard(trade_id: str, scale_up: bool = False) -> dict:
    """
    Build the inline keyboard for a new trade signal approval.

    Row 1: APPROVE | REJECT
    Row 2: SCALE x2  (only if scale_up=True, i.e. confidence is VERY HIGH)
    """
    rows = [
        [
            {"text": "✅ APPROVE", "callback_data": f"approve:{trade_id}"},
            {"text": "❌ REJECT",  "callback_data": f"reject:{trade_id}"},
        ]
    ]
    if scale_up:
        rows.append([
            {"text": "📈 SCALE x2", "callback_data": f"scale:{trade_id}"},
        ])

    return {"inline_keyboard": rows}


def _trade_mgmt_keyboard(trade_id: str) -> dict:
    """
    Build the inline keyboard for active trade management.

    Row 1: TIGHTEN SL  |  TRAIL SL
    Row 2: CLOSE NOW
    """
    return {
        "inline_keyboard": [
            [
                {"text": "🔒 Tighten SL", "callback_data": f"tighten_sl:{trade_id}"},
                {"text": "📈 Trail SL",   "callback_data": f"trail_sl:{trade_id}"},
            ],
            [
                {"text": "❌ Close Now",  "callback_data": f"close:{trade_id}"},
            ],
        ]
    }


# =========================
# APPROVAL BOT CLASS
# =========================

class TelegramApprovalBot:
    """
    Manages all Telegram inline keyboard approval flows for TradingBot.

    One shared instance in trading_engine.py.

    Key public methods
    ------------------
    send_signal_request(signal, trade_id)
        → blocks until APPROVE/REJECT/SCALE or timeout
        → returns CallbackOutcome

    send_trade_mgmt_request(trade, reason)
        → sends active trade management keyboard
        → returns immediately (non-blocking, outcome handled in poller)

    process_callback(update)
        → called by the Telegram poller thread with each incoming update
        → resolves pending requests

    send_trade_update(text)
        → sends a plain status message (no keyboard) — targets hit, SL hit, etc.
    """

    def __init__(self) -> None:
        self._pending:     Dict[str, _PendingRequest] = {}
        self._pending_lock = threading.Lock()

        # Background thread to expire stale requests
        self._expiry_thread = threading.Thread(
            target=self._expiry_loop,
            daemon=True,
            name="TGBotExpiryThread",
        )
        self._expiry_thread.start()

        # Track getUpdates offset to avoid re-processing
        self._update_offset: int = 0

        logger.info(
            "[TGBot] TelegramApprovalBot initialised | timeout=%d min | chat=%s",
            TELEGRAM_APPROVAL_TIMEOUT_MINUTES, CHAT_ID,
        )

    # ------------------------------------------------------------------
    # PUBLIC: SIGNAL APPROVAL (BLOCKING)
    # ------------------------------------------------------------------

    def send_signal_request(
        self,
        signal: dict,
        trade_id: str,
    ) -> CallbackOutcome:
        """
        Send a trade signal to Telegram with inline approval keyboard.
        BLOCKS until the user taps a button or the request expires.

        Parameters
        ----------
        signal    : dict from SignalEngine.compute() — must contain trade details
        trade_id  : unique trade identifier (e.g. "PAPER-20260520-001")

        Returns
        -------
        CallbackOutcome: APPROVED / REJECTED / SCALED / EXPIRED
        """
        # ---- Build message text ----
        direction  = signal.get("direction", "?")
        trade_sig  = signal.get("trade_signal", "?")
        conf_level = signal.get("confidence_level", "?")
        adj_score  = signal.get("adjusted_score", 0)
        alignment  = signal.get("alignment_summary", "?")
        price      = signal.get("price")
        vwap       = signal.get("vwap")
        ema9       = signal.get("ema9")

        price_str = f"₹{price:,.2f}" if price else "?"
        vwap_str  = f"₹{vwap:,.2f}" if vwap else "?"
        ema9_str  = f"₹{ema9:,.2f}" if ema9 else "?"

        emoji = "🟢" if direction == "BULLISH" else ("🔴" if direction == "BEARISH" else "⚪")

        text = (
            f"{emoji} <b>TRADE SIGNAL — {direction}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Signal:</b> {trade_sig}\n"
            f"<b>Stop Loss:</b> {signal.get('stop_loss', '?')} ({STOP_LOSS_POINTS} pts)\n"
            f"<b>Target 1:</b> {signal.get('target1', '?')}\n"
            f"<b>Target 2:</b> {signal.get('target2', '?')}\n"
            f"<b>Target 3:</b> {signal.get('target3', '?')}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Price:</b> {price_str} | <b>VWAP:</b> {vwap_str} | <b>EMA9:</b> {ema9_str}\n"
            f"<b>MTF:</b> {alignment}\n"
            f"<b>Confidence:</b> {conf_level} ({adj_score}/100)\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Trade ID:</b> <code>{trade_id}</code>\n"
            f"<i>⏱ Auto-expires in {TELEGRAM_APPROVAL_TIMEOUT_MINUTES} min</i>"
        )

        scale_up = signal.get("scale_up", False)
        keyboard  = _signal_keyboard(trade_id, scale_up=scale_up)

        # ---- Send message ----
        message_id = _send_message(text, reply_markup=keyboard)
        if message_id is None:
            logger.error("[TGBot] Failed to send signal request to Telegram")
            return CallbackOutcome.EXPIRED

        # ---- Register pending request ----
        timeout_s  = TELEGRAM_APPROVAL_TIMEOUT_MINUTES * 60
        pending    = _PendingRequest(
            trade_id    = trade_id,
            message_id  = message_id,
            expires_at  = time.time() + timeout_s,
        )
        with self._pending_lock:
            self._pending[trade_id] = pending

        logger.info(
            "[TGBot] Signal request sent | trade_id=%s | msg_id=%d | timeout=%ds",
            trade_id, message_id, timeout_s,
        )

        # ---- Wait for user response (blocking) ----
        resolved = pending.event.wait(timeout=timeout_s + 5)  # +5s buffer

        if not resolved or pending.outcome is None:
            # Remove from pending dict
            with self._pending_lock:
                self._pending.pop(trade_id, None)
            # Edit message to show expired
            _edit_message(
                message_id,
                f"⏱ <b>EXPIRED</b> — Signal not approved in time.\n"
                f"Trade ID: <code>{trade_id}</code>",
            )
            logger.info("[TGBot] Request EXPIRED | trade_id=%s", trade_id)
            return CallbackOutcome.EXPIRED

        return pending.outcome

    # ------------------------------------------------------------------
    # PUBLIC: ACTIVE TRADE MANAGEMENT (NON-BLOCKING)
    # ------------------------------------------------------------------

    def send_trade_mgmt_request(
        self,
        trade: dict,
        reason: str = "",
    ) -> Optional[int]:
        """
        Send an active trade management keyboard (non-blocking).

        Used when the tracker loop detects a significant condition:
          - Price approaching SL
          - Target level hit (suggest trailing)
          - End-of-day approaching

        Parameters
        ----------
        trade  : trade dict (from trade_state.py / trade log)
        reason : why the management prompt is being shown

        Returns
        -------
        message_id (int) of the sent message — for later edits.
        None on failure.
        """
        trade_id   = trade.get("trade_id", "?")
        direction  = trade.get("direction", "?")
        entry      = trade.get("entry_price", 0)
        current    = trade.get("current_price", 0)
        pts_pnl    = round(current - entry, 2) if direction == "BULLISH" else round(entry - current, 2)
        emoji      = "🟢" if pts_pnl >= 0 else "🔴"

        text = (
            f"⚙️ <b>TRADE MANAGEMENT</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Trade:</b> {trade.get('trade_signal', '?')}\n"
            f"<b>Entry:</b> ₹{entry:,.2f} → <b>Now:</b> ₹{current:,.2f}\n"
            f"{emoji} <b>P&L:</b> {pts_pnl:+.2f} pts\n"
            f"<b>SL:</b> {STOP_LOSS_POINTS} pts from entry\n"
        )
        if reason:
            text += f"<i>ℹ️ {reason}</i>\n"

        text += (
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Trade ID:</b> <code>{trade_id}</code>"
        )

        keyboard   = _trade_mgmt_keyboard(trade_id)
        message_id = _send_message(text, reply_markup=keyboard)

        if message_id:
            # Register as non-blocking pending (no event.wait())
            pending = _PendingRequest(
                trade_id   = trade_id,
                message_id = message_id,
                expires_at = time.time() + 600,  # 10-min window for management actions
            )
            with self._pending_lock:
                # Use a composite key for management requests
                self._pending[f"mgmt:{trade_id}"] = pending

            logger.info("[TGBot] Trade mgmt request sent | trade_id=%s | msg_id=%d", trade_id, message_id)
        else:
            logger.error("[TGBot] Failed to send trade mgmt request | trade_id=%s", trade_id)

        return message_id

    # ------------------------------------------------------------------
    # PUBLIC: PROCESS INCOMING CALLBACK (called by poller thread)
    # ------------------------------------------------------------------

    def process_callback(self, update: dict) -> Optional[str]:
        """
        Process one Telegram update from the polling loop.

        Called by trading_engine.py's telegram_poller thread for every
        incoming update from getUpdates.

        Parameters
        ----------
        update : dict — raw Telegram update object

        Returns
        -------
        str — the callback_data if processed, None if not a relevant callback
        """
        # ---- Only handle callback_query updates ----
        cb = update.get("callback_query")
        if not cb:
            return None

        cb_id     = cb.get("id", "")
        from_id   = str(cb.get("from", {}).get("id", ""))
        data      = cb.get("data", "")
        msg_id    = cb.get("message", {}).get("message_id")

        # ---- Security: only accept callbacks from authorised CHAT_ID ----
        if from_id != str(CHAT_ID):
            logger.warning("[TGBot] Callback from unauthorised user %s — ignoring", from_id)
            answer_callback_query(cb_id, "❌ Not authorised")
            return None

        # ---- Parse callback_data: "action:trade_id" ----
        if ":" not in data:
            logger.warning("[TGBot] Malformed callback data: %s", data)
            answer_callback_query(cb_id, "⚠️ Invalid callback")
            return None

        action, trade_id = data.split(":", 1)
        action = action.lower().strip()

        # ---- Map action to CallbackOutcome ----
        _ACTION_MAP = {
            "approve":    CallbackOutcome.APPROVED,
            "reject":     CallbackOutcome.REJECTED,
            "scale":      CallbackOutcome.SCALED,
            "tighten_sl": CallbackOutcome.TIGHTEN_SL,
            "trail_sl":   CallbackOutcome.TRAIL_SL,
            "close":      CallbackOutcome.CLOSE_NOW,
        }

        outcome = _ACTION_MAP.get(action)
        if outcome is None:
            logger.warning("[TGBot] Unknown action '%s' in callback", action)
            answer_callback_query(cb_id, "⚠️ Unknown action")
            return None

        # ---- Find matching pending request ----
        resolved = False
        with self._pending_lock:
            # Try direct key first (signal approval), then mgmt key
            for key in [trade_id, f"mgmt:{trade_id}"]:
                pending = self._pending.get(key)
                if pending:
                    pending.outcome = outcome
                    pending.event.set()
                    self._pending.pop(key, None)
                    resolved = True
                    break

        if not resolved:
            logger.warning(
                "[TGBot] No pending request for trade_id=%s (may have already expired)",
                trade_id,
            )
            answer_callback_query(cb_id, "⏱ Already resolved or expired")
            return data

        # ---- Acknowledge button press ----
        outcome_labels = {
            CallbackOutcome.APPROVED:   "✅ Trade APPROVED — placing order",
            CallbackOutcome.REJECTED:   "❌ Trade REJECTED",
            CallbackOutcome.SCALED:     "📈 Trade APPROVED with 2x scale-up",
            CallbackOutcome.TIGHTEN_SL: "🔒 SL tightened",
            CallbackOutcome.TRAIL_SL:   "📈 SL trailing activated",
            CallbackOutcome.CLOSE_NOW:  "❌ Trade closed by user",
        }
        answer_callback_query(cb_id, outcome_labels.get(outcome, "✅ Processed"))

        # ---- Edit original message to show outcome ----
        outcome_icons = {
            CallbackOutcome.APPROVED:   "✅",
            CallbackOutcome.REJECTED:   "❌",
            CallbackOutcome.SCALED:     "📈",
            CallbackOutcome.TIGHTEN_SL: "🔒",
            CallbackOutcome.TRAIL_SL:   "📈",
            CallbackOutcome.CLOSE_NOW:  "❌",
        }
        icon     = outcome_icons.get(outcome, "✅")
        label    = outcome_labels.get(outcome, "Processed")
        ts       = datetime.now().strftime("%H:%M:%S")

        if msg_id:
            _edit_message(
                msg_id,
                f"{icon} <b>{label}</b>\n"
                f"Trade ID: <code>{trade_id}</code>\n"
                f"<i>Processed at {ts}</i>",
            )

        logger.info(
            "[TGBot] Callback processed | trade_id=%s | action=%s | outcome=%s",
            trade_id, action, outcome.value,
        )
        return data

    # ------------------------------------------------------------------
    # PUBLIC: POLL FOR UPDATES (called by poller thread)
    # ------------------------------------------------------------------

    def poll_updates(self) -> None:
        """
        Fetch pending Telegram updates and process callbacks.

        Called by trading_engine.py's telegram_poller thread every
        TELEGRAM_POLL_INTERVAL_SECONDS (3s). Uses long-poll getUpdates
        with timeout=2s per request to minimise unnecessary API calls.
        """
        params = {
            "offset":          self._update_offset,
            "timeout":         2,
            "allowed_updates": ["callback_query"],
        }
        resp = _tg_get("getUpdates", params)
        if not resp or not resp.get("ok"):
            return

        updates = resp.get("result", [])
        for update in updates:
            update_id             = update.get("update_id", 0)
            self._update_offset   = max(self._update_offset, update_id + 1)
            self.process_callback(update)

    # ------------------------------------------------------------------
    # PUBLIC: PLAIN MESSAGE SENDER
    # ------------------------------------------------------------------

    def send_trade_update(self, text: str) -> None:
        """
        Send a plain status message to Telegram (no keyboard).

        Used for:
          - Target hit notifications
          - SL hit notifications
          - EOD trade closure
          - Error alerts
          - Daily session start/end
        """
        try:
            _send_message(text)
            logger.debug("[TGBot] Status message sent: %s", text[:80])
        except Exception as exc:
            logger.error("[TGBot] Failed to send status message: %s", exc)

    def send_startup_message(self) -> None:
        """Send a session-start notification to Telegram."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.send_trade_update(
            f"🚀 <b>TradingBot STARTED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Mode: 📄 Paper Trading\n"
            f"Data: 📊 API (yfinance + NSE)\n"
            f"Started at: {now}\n"
            f"<i>Monitoring NIFTY intraday signals...</i>"
        )

    def send_shutdown_message(self) -> None:
        """Send a session-end notification to Telegram."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.send_trade_update(
            f"🛑 <b>TradingBot STOPPED</b>\n"
            f"Stopped at: {now}"
        )

    # ------------------------------------------------------------------
    # INTERNAL: EXPIRY LOOP (background thread)
    # ------------------------------------------------------------------

    def _expiry_loop(self) -> None:
        """
        Background thread that removes expired pending requests.
        Runs every 30 seconds and cleans up any request past its expires_at.
        """
        while True:
            time.sleep(30)
            now = time.time()
            expired_keys = []

            with self._pending_lock:
                for key, pending in self._pending.items():
                    if now >= pending.expires_at and not pending.event.is_set():
                        expired_keys.append(key)

            for key in expired_keys:
                with self._pending_lock:
                    pending = self._pending.pop(key, None)

                if pending:
                    pending.outcome = CallbackOutcome.EXPIRED
                    pending.event.set()
                    _edit_message(
                        pending.message_id,
                        f"⏱ <b>EXPIRED</b> — No response received.\n"
                        f"Trade ID: <code>{pending.trade_id}</code>",
                    )
                    logger.info(
                        "[TGBot] Auto-expired request | trade_id=%s | key=%s",
                        pending.trade_id, key,
                    )
