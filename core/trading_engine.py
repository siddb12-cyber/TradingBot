"""
core/trading_engine.py
======================
Unified single-process trading engine for TradingBot.

Replaces the fragile multi-process architecture (main.py spawning separate
ai_trading_assistant and live_trade_tracker processes) with a robust
single-process, 3-thread design:

  Thread 1 — signal_loop()     (every 5 min)
      Generates trade signals via SignalEngine (API-based, no OCR).
      Sends Telegram inline keyboard approval request.
      Opens trade after APPROVE, or cancels after REJECT/EXPIRE.

  Thread 2 — tracker_loop()    (every 60s)
      Fetches live NIFTY price via DataEngine.get_live_price().
      Monitors active trade against SL / T1 / T2 / T3.
      Sends Telegram notifications on target/SL hits.
      Auto-closes at EOD (15:29 IST).

  Thread 3 — telegram_poller() (every 3s)
      Calls TelegramApprovalBot.poll_updates() to receive inline keyboard
      callback_queries and route them to the correct pending request.

Engine State Machine
--------------------
  IDLE              — no active trade, no pending approval
  PENDING_APPROVAL  — signal generated, waiting for Telegram APPROVE/REJECT
  TRADE_ACTIVE      — trade is open, tracker is monitoring

All state transitions are guarded by a threading.Lock.

Why single-process?
-------------------
Multi-process crashed because:
  - Shared JSON state file had race conditions on Windows
  - One crashed process killed the other's assumptions
  - No cross-process error recovery

Single-process gives:
  - Shared in-memory state (no race conditions)
  - One crash is caught by the thread's except block (not fatal to others)
  - Clean startup/shutdown with threading.Event

Safety
------
- PAPER_TRADING_MODE = True by default (hardcoded in config.py)
- Groww execution only fires if PAPER_TRADING_MODE = False (requires code change)
- Every decision is logged to DecisionLogger (daily Excel)
- All trade approvals require user action on Telegram
"""

import logging
import threading
import time
import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

from config.config import (
    configure_logging,
    PAPER_TRADING_MODE,
    SCAN_INTERVAL_SECONDS,
    TRACKER_INTERVAL_SECONDS,
    TELEGRAM_POLL_INTERVAL_SECONDS,
    STOP_LOSS_POINTS,
    TARGET_1_POINTS,
    TARGET_2_POINTS,
    TARGET_3_POINTS,
    CONFIDENCE_HIGH_THRESHOLD,
    CONFIDENCE_MED_THRESHOLD,
    PAPER_SESSION_PREFIX,
    TRADE_LOG_DIR,
)
from core.data_engine import DataEngine
from core.signal_engine import SignalEngine, CONFIDENCE_LOW, CONFIDENCE_VERY_HIGH
from core.telegram_approval_bot import TelegramApprovalBot, CallbackOutcome
from core.trade_state import TradeStateManager
from core.risk_engine import RiskEngine
from core.market_hours import is_market_open, is_eod_close_time, seconds_until_next_open
from analytics.decision_logger import DecisionLogger

# Groww executor import — gracefully handles ImportError if playwright not installed
try:
    from core.groww_executor import GrowwExecutor
    _GROWW_AVAILABLE = True
except ImportError:
    _GROWW_AVAILABLE = False
    GrowwExecutor = None  # type: ignore

# Weekly report trigger
try:
    from analytics.weekly_report import generate_and_send as generate_weekly_report
    _WEEKLY_REPORT_AVAILABLE = True
except ImportError:
    _WEEKLY_REPORT_AVAILABLE = False

# =========================
# MODULE LOGGER
# =========================

logger = logging.getLogger(__name__)

# =========================
# ENGINE STATE
# =========================

class EngineState(Enum):
    """Internal state machine for the trading engine."""
    IDLE             = "IDLE"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    TRADE_ACTIVE     = "TRADE_ACTIVE"


# =========================
# TRADING ENGINE
# =========================

class TradingEngine:
    """
    Single-process trading engine with 3 threads:
      1. signal_loop    — market scanning + Telegram approval (every 5m)
      2. tracker_loop   — live P&L monitoring (every 60s)
      3. telegram_poller — callback query polling (every 3s)

    Usage
    -----
    engine = TradingEngine()
    engine.start()          # Launch all threads and block until stopped
    engine.stop()           # Graceful shutdown (signal all threads to exit)
    """

    def __init__(self) -> None:
        # ---- Core components ----
        self._data      = DataEngine()
        self._signal    = SignalEngine(self._data)
        self._tgbot     = TelegramApprovalBot()
        self._state     = TradeStateManager()
        self._risk      = RiskEngine()
        self._dlogger   = DecisionLogger()

        # ---- Groww executor (only used when PAPER_TRADING_MODE=False) ----
        self._groww: Optional[object] = None
        if not PAPER_TRADING_MODE and _GROWW_AVAILABLE:
            self._groww = GrowwExecutor()
            logger.warning("[Engine] ⚠️  LIVE TRADING MODE — Groww executor active")
        else:
            logger.info("[Engine] 📄 Paper trading mode — Groww executor disabled")

        # ---- Engine state machine ----
        self._engine_state      = EngineState.IDLE
        self._engine_state_lock = threading.Lock()
        self._current_trade_id: Optional[str] = None
        self._pending_signal: Optional[dict]   = None

        # ---- Shutdown signal ----
        self._stop_event = threading.Event()

        # ---- Weekly report sentinel ----
        self._weekly_report_fired: set = set()

        # ---- Scan counter (for DecisionLogger) ----
        self._scan_number: int = 0

        logger.info(
            "[Engine] TradingEngine initialised | mode=%s | SCAN=%ds | TRACKER=%ds | POLLER=%ds",
            "PAPER" if PAPER_TRADING_MODE else "LIVE",
            SCAN_INTERVAL_SECONDS,
            TRACKER_INTERVAL_SECONDS,
            TELEGRAM_POLL_INTERVAL_SECONDS,
        )

    # ==================================================================
    # PUBLIC: START / STOP
    # ==================================================================

    def start(self) -> None:
        """
        Launch all three threads and block until stop() is called.

        Call this from main.py as the single entry point.
        """
        configure_logging()

        logger.info("=" * 60)
        logger.info("[Engine] TradingBot starting up")
        logger.info("[Engine] Mode: %s", "PAPER TRADING" if PAPER_TRADING_MODE else "⚠️ LIVE TRADING")
        logger.info("=" * 60)

        # ---- Health check before starting loops ----
        health = self._data.health_check()
        logger.info("[Engine] Data health: %s", health["status"])
        if health["status"] == "UNHEALTHY":
            logger.error("[Engine] Data sources unavailable — cannot start. Retrying in 60s...")
            time.sleep(60)
            # Retry once
            health = self._data.health_check()
            if health["status"] == "UNHEALTHY":
                logger.critical("[Engine] Data still unavailable — aborting startup")
                self._tgbot.send_trade_update(
                    "🚨 <b>TradingBot startup FAILED</b>\n"
                    "Data sources (NSE + yfinance) are unavailable.\n"
                    "Please check your internet connection."
                )
                return

        # ---- Reload state from disk (survive restarts) ----
        self._state.reload()
        self._risk.reload()

        if self._state.has_active_trade():
            self._set_engine_state(EngineState.TRADE_ACTIVE)
            trade = self._state.get_active_trade()
            self._current_trade_id = trade.get("trade_id")
            logger.info("[Engine] Resumed with active trade: %s", self._current_trade_id)

        # ---- Send startup notification ----
        self._tgbot.send_startup_message()

        # ---- Launch threads ----
        threads = [
            threading.Thread(target=self._signal_loop,     name="SignalLoop",    daemon=True),
            threading.Thread(target=self._tracker_loop,    name="TrackerLoop",   daemon=True),
            threading.Thread(target=self._telegram_poller, name="TGPoller",      daemon=True),
        ]
        for t in threads:
            t.start()
            logger.info("[Engine] Thread started: %s", t.name)

        # ---- Block main thread until stop() is called ----
        try:
            while not self._stop_event.is_set():
                time.sleep(1)
                self._maybe_trigger_weekly_report()
        except KeyboardInterrupt:
            logger.info("[Engine] KeyboardInterrupt received — shutting down")
        finally:
            self.stop()

    def stop(self) -> None:
        """Signal all threads to exit gracefully."""
        logger.info("[Engine] Stopping all threads...")
        self._stop_event.set()
        self._tgbot.send_shutdown_message()

    # ==================================================================
    # THREAD 1: SIGNAL LOOP
    # ==================================================================

    def _signal_loop(self) -> None:
        """
        Signal generation loop — runs every SCAN_INTERVAL_SECONDS (5 min).

        Logic per iteration:
          1. Wait until market is open
          2. Skip if engine is not IDLE (trade active or pending approval)
          3. Compute signal via SignalEngine
          4. Log decision (DecisionLogger)
          5. Check risk engine gate
          6. Send Telegram approval request (blocking until user responds)
          7. On APPROVE: open trade
          8. On REJECT/EXPIRE: stay IDLE
        """
        logger.info("[SignalLoop] Thread started")

        while not self._stop_event.is_set():
            try:
                self._signal_cycle()
            except Exception as exc:
                logger.error("[SignalLoop] Unhandled error in cycle: %s", exc, exc_info=True)

            # ---- Sleep until next scan ----
            self._interruptible_sleep(SCAN_INTERVAL_SECONDS)

        logger.info("[SignalLoop] Thread exited")

    def _signal_cycle(self) -> None:
        """Run one complete signal scan cycle."""

        # ================================================================
        # GATE 1: Market hours
        # ================================================================
        if not is_market_open():
            next_open_s = seconds_until_next_open()
            logger.info("[SignalLoop] Market closed — sleeping %ds until next open", next_open_s)
            self._interruptible_sleep(min(next_open_s, SCAN_INTERVAL_SECONDS))
            return

        # ================================================================
        # GATE 2: Engine state — only scan when IDLE
        # ================================================================
        with self._engine_state_lock:
            current_state = self._engine_state

        if current_state == EngineState.PENDING_APPROVAL:
            logger.info("[SignalLoop] Pending approval — skip scan")
            return

        if current_state == EngineState.TRADE_ACTIVE:
            logger.info("[SignalLoop] Trade active — skip scan (tracker monitoring)")
            self._dlogger.log(
                decision="ACTIVE TRADE",
                reason="Trade already open — signal scan skipped",
                scan_number=self._next_scan(),
            )
            return

        # ================================================================
        # GATE 3: Generate signal
        # ================================================================
        self._scan_number = self._next_scan()
        logger.info("[SignalLoop] === SCAN #%d ===", self._scan_number)

        sig = self._signal.compute()

        # ================================================================
        # GATE 4: Data validity
        # ================================================================
        if not sig.get("valid", False):
            reason = sig.get("_reason", "Data fetch error")
            logger.warning("[SignalLoop] Invalid data — %s", reason)
            self._dlogger.log(
                decision="OCR ERROR",
                reason=reason,
                mtf=sig,
                scan_number=self._scan_number,
            )
            return

        # ================================================================
        # GATE 5: Direction / is_trade
        # ================================================================
        if not sig.get("is_trade", False):
            reason = sig.get("_reason", f"Direction is {sig.get('direction', 'SIDEWAYS')}")
            logger.info("[SignalLoop] No trade — %s", reason)
            self._dlogger.log(
                decision="SIDEWAYS",
                reason=reason,
                mtf=sig,
                oi_result=sig.get("oi_result", {}),
                sent_result=sig.get("sent_result", {}),
                base_score=sig.get("base_score", 0),
                adjusted_score=sig.get("adjusted_score", 0),
                confidence_level=sig.get("confidence_level", "LOW"),
                scan_number=self._scan_number,
            )
            return

        # ================================================================
        # GATE 6: Confidence threshold
        # ================================================================
        confidence_level = sig.get("confidence_level", CONFIDENCE_LOW)
        adjusted_score   = sig.get("adjusted_score", 0)

        if confidence_level == CONFIDENCE_LOW:
            reason = f"Confidence too low: {confidence_level} ({adjusted_score}/100)"
            logger.info("[SignalLoop] %s", reason)
            self._dlogger.log(
                decision="LOW CONFIDENCE",
                reason=reason,
                mtf=sig,
                oi_result=sig.get("oi_result", {}),
                sent_result=sig.get("sent_result", {}),
                base_score=sig.get("base_score", 0),
                adjusted_score=adjusted_score,
                confidence_level=confidence_level,
                scan_number=self._scan_number,
            )
            return

        # ================================================================
        # GATE 7: Risk engine
        # ================================================================
        self._risk.reload()
        risk_check = self._risk.check_trade_allowed(
            direction        = sig["direction"],
            confidence_level = confidence_level,
        )

        if not risk_check.get("allowed", False):
            reason = risk_check.get("reason", "Risk engine blocked trade")
            logger.info("[SignalLoop] Risk blocked: %s", reason)
            self._dlogger.log(
                decision="BLOCKED",
                reason=reason,
                mtf=sig,
                oi_result=sig.get("oi_result", {}),
                sent_result=sig.get("sent_result", {}),
                base_score=sig.get("base_score", 0),
                adjusted_score=adjusted_score,
                confidence_level=confidence_level,
                scan_number=self._scan_number,
            )
            return

        # ================================================================
        # GATE 8: Generate trade ID and enter PENDING_APPROVAL
        # ================================================================
        trade_id = self._generate_trade_id()
        self._set_engine_state(EngineState.PENDING_APPROVAL)
        self._pending_signal = sig

        logger.info(
            "[SignalLoop] Signal ready | %s | %s | score=%d | trade_id=%s",
            sig["direction"], sig["trade_signal"], adjusted_score, trade_id,
        )

        # ================================================================
        # GATE 9: Telegram approval (BLOCKING — waits for user tap)
        # ================================================================
        outcome = self._tgbot.send_signal_request(sig, trade_id)
        logger.info("[SignalLoop] Telegram outcome: %s | trade_id=%s", outcome.value, trade_id)

        # ================================================================
        # PROCESS OUTCOME
        # ================================================================
        if outcome == CallbackOutcome.APPROVED or outcome == CallbackOutcome.SCALED:
            scale_lots = sig.get("scale_up_lots", 1) if outcome == CallbackOutcome.SCALED else 1
            self._open_trade(sig, trade_id, scale_lots)

        else:
            # REJECTED or EXPIRED
            reason = f"Telegram {outcome.value} by user"
            logger.info("[SignalLoop] Trade not opened — %s", reason)
            self._dlogger.log(
                decision="BLOCKED",
                reason=reason,
                trade_id=trade_id,
                mtf=sig,
                oi_result=sig.get("oi_result", {}),
                sent_result=sig.get("sent_result", {}),
                base_score=sig.get("base_score", 0),
                adjusted_score=adjusted_score,
                confidence_level=confidence_level,
                scan_number=self._scan_number,
            )
            self._set_engine_state(EngineState.IDLE)
            self._pending_signal = None

    def _open_trade(self, sig: dict, trade_id: str, lots: int = 1) -> None:
        """Open a trade after Telegram approval."""
        price = sig.get("price") or 0
        vwap  = sig.get("vwap")  or 0
        ema9  = sig.get("ema9")  or 0

        # ---- Open in state machine ----
        try:
            actual_trade_id = self._state.open_trade(
                signal      = sig["trade_signal"],
                entry_price = price,
                trend       = sig["trend"],
                vwap        = vwap,
                ema9        = ema9,
                lots        = lots,
            )
            self._risk.record_trade_opened()
            # Write opening row to trade_log_YYYY-MM-DD.xlsx
            self._state.create_excel_row(sig, lots=lots)
        except Exception as exc:
            logger.error("[SignalLoop] Failed to open trade state: %s", exc)
            self._set_engine_state(EngineState.IDLE)
            self._pending_signal = None
            return

        self._current_trade_id = actual_trade_id
        self._set_engine_state(EngineState.TRADE_ACTIVE)
        self._pending_signal = None

        # ---- DecisionLogger ----
        self._dlogger.log(
            decision         = "TRADE SIGNAL",
            reason           = f"APPROVED | {sig['direction']} | {sig['alignment_summary']}",
            trade_id         = actual_trade_id,
            mtf              = sig,
            oi_result        = sig.get("oi_result", {}),
            sent_result      = sig.get("sent_result", {}),
            base_score       = sig.get("base_score", 0),
            adjusted_score   = sig.get("adjusted_score", 0),
            confidence_level = sig.get("confidence_level", ""),
            scan_number      = self._scan_number,
        )

        # ---- Groww execution (paper-safe) ----
        if not PAPER_TRADING_MODE and self._groww:
            logger.info("[SignalLoop] Executing LIVE order on Groww | lots=%d", lots)
            try:
                self._groww.execute_order(sig, lots=lots)
            except Exception as exc:
                logger.error("[SignalLoop] Groww execution failed: %s", exc)
                self._tgbot.send_trade_update(
                    f"🚨 <b>Groww execution FAILED</b>\n"
                    f"Error: {exc}\n"
                    f"Trade <code>{actual_trade_id}</code> is open in state but NO order was placed."
                )
        else:
            logger.info("[SignalLoop] Paper mode — Groww execution skipped")

        # ---- Telegram confirmation ----
        lots_str = f" (x{lots} lots)" if lots > 1 else ""
        self._tgbot.send_trade_update(
            f"✅ <b>TRADE OPENED{lots_str}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Signal:</b> {sig['trade_signal']}\n"
            f"<b>Entry:</b> ₹{price:,.2f}\n"
            f"<b>SL:</b> {STOP_LOSS_POINTS} pts (₹{_sl_inr:,.0f} risk) | "
            f"<b>T1:</b> {TARGET_1_POINTS} pts | "
            f"<b>T2:</b> {TARGET_2_POINTS} pts | "
            f"<b>T3:</b> {TARGET_3_POINTS} pts (₹{_t3_inr:,.0f} gain)\n"
            f"<b>MTF:</b> {sig.get('alignment_summary', '?')}\n"
            f"<b>Confidence:</b> {sig.get('confidence_level', '?')} ({sig.get('adjusted_score', 0)}/100)\n"
            f"<b>Mode:</b> {'📄 Paper' if PAPER_TRADING_MODE else '💰 Live'}\n"
            f"<b>Trade ID:</b> <code>{actual_trade_id}</code>"
        )

        logger.info(
            "[SignalLoop] Trade opened | id=%s | signal=%s | entry=%.2f | lots=%d",
            actual_trade_id, sig["trade_signal"], price, lots,
        )

    # ==================================================================
    # THREAD 2: TRACKER LOOP
    # ==================================================================

    def _tracker_loop(self) -> None:
        """
        Live trade tracker — runs every TRACKER_INTERVAL_SECONDS (60s).

        Monitors:
          - Current NIFTY price vs entry price
          - Target 1, 2, 3 hit detection (NIFTY point moves)
          - Stop loss hit detection
          - EOD auto-close at 15:29 IST
        """
        logger.info("[Tracker] Thread started")

        while not self._stop_event.is_set():
            try:
                self._tracker_cycle()
            except Exception as exc:
                logger.error("[Tracker] Unhandled error: %s", exc, exc_info=True)

            self._interruptible_sleep(TRACKER_INTERVAL_SECONDS)

        logger.info("[Tracker] Thread exited")

    def _tracker_cycle(self) -> None:
        """One tracker monitoring cycle."""

        # ---- Only run when there is an active trade ----
        self._state.reload()
        if not self._state.has_active_trade():
            return

        trade = self._state.get_active_trade()
        if not trade:
            return

        entry_price = trade.get("entry_price", 0)
        direction   = trade.get("direction", "CE")   # "CE" = bullish, "PE" = bearish
        trade_id    = trade.get("trade_id", "?")
        milestones  = trade.get("milestones_hit", [])

        # ---- Fetch live price ----
        current_price = self._data.get_live_price()
        if current_price is None:
            logger.warning("[Tracker] Could not get live price — skipping cycle")
            return

        # ---- Compute points P&L ----
        if direction == "CE":
            pts_pnl = current_price - entry_price
        else:
            pts_pnl = entry_price - current_price

        logger.info(
            "[Tracker] Trade %s | entry=%.2f now=%.2f | P&L=%+.2f pts",
            trade_id, entry_price, current_price, pts_pnl,
        )

        # ================================================================
        # EOD AUTO-CLOSE (15:29 IST)
        # ================================================================
        if is_eod_close_time():
            logger.info("[Tracker] EOD auto-close triggered")
            self._close_trade(
                current_price = current_price,
                pts_pnl       = pts_pnl,
                outcome       = "EOD_CLOSE",
                reason        = "End of day auto-close at 15:29 IST",
            )
            return

        # ================================================================
        # STOP LOSS CHECK
        # ================================================================
        if pts_pnl <= -STOP_LOSS_POINTS:
            logger.info("[Tracker] SL HIT at %.2f | loss=%.2f pts", current_price, pts_pnl)
            self._close_trade(
                current_price = current_price,
                pts_pnl       = pts_pnl,
                outcome       = "SL_HIT",
                reason        = f"Stop loss hit at {current_price:.2f} ({pts_pnl:+.2f} pts)",
            )
            return

        # ================================================================
        # TARGET CHECKS (T3 → T2 → T1 in order of priority)
        # ================================================================
        if pts_pnl >= TARGET_3_POINTS and "T3_HIT" not in milestones:
            logger.info("[Tracker] TARGET 3 HIT | %.2f pts", pts_pnl)
            self._state.update_milestone("T3_HIT")
            self._close_trade(
                current_price = current_price,
                pts_pnl       = pts_pnl,
                outcome       = "T3_HIT",
                reason        = f"Target 3 ({TARGET_3_POINTS} pts) hit at {current_price:.2f}",
            )
            self._tgbot.send_trade_update(
                f"🎯 <b>TARGET 3 HIT — FULL TARGET!</b>\n"
                f"Trade: <code>{trade_id}</code>\n"
                f"Price: ₹{current_price:,.2f} | P&L: +{pts_pnl:.2f} pts\n"
                f"✅ Trade closed"
            )
            return

        if pts_pnl >= TARGET_2_POINTS and "T2_HIT" not in milestones:
            logger.info("[Tracker] TARGET 2 HIT | %.2f pts", pts_pnl)
            self._state.update_milestone("T2_HIT")
            self._tgbot.send_trade_update(
                f"🎯 <b>TARGET 2 HIT</b>\n"
                f"Trade: <code>{trade_id}</code>\n"
                f"Price: ₹{current_price:,.2f} | P&L: +{pts_pnl:.2f} pts\n"
                f"<i>Still holding for T3...</i>"
            )
            # Send management keyboard
            trade["current_price"] = current_price
            self._tgbot.send_trade_mgmt_request(trade, reason="T2 hit — choose: hold for T3 or close now?")

        elif pts_pnl >= TARGET_1_POINTS and "T1_HIT" not in milestones:
            logger.info("[Tracker] TARGET 1 HIT | %.2f pts", pts_pnl)
            self._state.update_milestone("T1_HIT")
            self._tgbot.send_trade_update(
                f"🎯 <b>TARGET 1 HIT</b>\n"
                f"Trade: <code>{trade_id}</code>\n"
                f"Price: ₹{current_price:,.2f} | P&L: +{pts_pnl:.2f} pts\n"
                f"<i>Still holding for T2 ({TARGET_2_POINTS} pts)...</i>"
            )

        # ---- Regular status update every 5 minutes (on even minutes) ----
        elif datetime.now().minute % 5 == 0:
            emoji = "🟢" if pts_pnl >= 0 else "🔴"
            logger.info("[Tracker] Status tick | P&L=%+.2f pts", pts_pnl)
            # (No Telegram message every minute — only on targets/SL)

    def _close_trade(
        self,
        current_price: float,
        pts_pnl:       float,
        outcome:       str,
        reason:        str,
    ) -> None:
        """Close the active trade and reset engine state."""
        self._state.reload()
        if not self._state.has_active_trade():
            return

        trade    = self._state.get_active_trade()
        trade_id = trade.get("trade_id", "?")

        # ---- Determine win/loss ----
        pnl_outcome = "WIN" if pts_pnl > 0 else ("LOSS" if pts_pnl < 0 else "BREAKEVEN")

        # ---- Close in state machine ----
        self._state.close_trade(
            exit_price    = current_price,
            outcome       = outcome,
            points_result = pts_pnl,
        )
        self._risk.record_trade_closed(
            outcome       = pnl_outcome,
            points_result = pts_pnl,
            direction     = trade.get("direction", "CE"),
        )

        # ---- Update engine state ----
        self._set_engine_state(EngineState.IDLE)
        self._current_trade_id = None

        # ---- Telegram notification ----
        emoji    = "✅" if pts_pnl > 0 else ("❌" if pts_pnl < 0 else "⚪")
        _lots    = trade.get('lots', 1)
        _inr_pnl = pts_pnl * _lots * 75 * 0.5
        _inr_str = f"₹{abs(_inr_pnl):,.0f}"
        _sign    = '+' if _inr_pnl >= 0 else '-'
        self._tgbot.send_trade_update(
            f"{emoji} <b>TRADE CLOSED — {outcome}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Trade:</b> {trade.get('signal', '?')} | Lots: {_lots}\n"
            f"<b>Entry:</b> ₹{trade.get('entry_price', 0):,.2f} → "
            f"<b>Exit:</b> ₹{current_price:,.2f}\n"
            f"<b>P&L:</b> {pts_pnl:+.2f} pts | {_sign}{_inr_str} | {pnl_outcome}\n"
            f"<b>Reason:</b> {reason}\n"
            f"<b>Trade ID:</b> <code>{trade_id}</code>"
        )

        logger.info(
            "[Tracker] Trade closed | id=%s | outcome=%s | pts=%+.2f",
            trade_id, outcome, pts_pnl,
        )

    # ==================================================================
    # THREAD 3: TELEGRAM POLLER
    # ==================================================================

    def _telegram_poller(self) -> None:
        """
        Telegram callback polling thread — runs every TELEGRAM_POLL_INTERVAL_SECONDS (3s).

        Calls TelegramApprovalBot.poll_updates() which fetches any pending
        callback_query updates and routes them to the correct approval event.

        This thread also handles management callbacks (CLOSE_NOW, TIGHTEN_SL, etc.)
        by reading the resolved outcome from the TelegramApprovalBot.
        """
        logger.info("[Poller] Telegram poller thread started")

        while not self._stop_event.is_set():
            try:
                self._tgbot.poll_updates()
                # Check if any management callback resolved a close action
                self._process_mgmt_callbacks()
            except Exception as exc:
                logger.error("[Poller] Error: %s", exc)

            self._interruptible_sleep(TELEGRAM_POLL_INTERVAL_SECONDS)

        logger.info("[Poller] Telegram poller thread exited")

    def _process_mgmt_callbacks(self) -> None:
        """
        Check if any management callback (CLOSE_NOW etc.) was processed.
        Management callbacks are non-blocking — we check after each poll cycle.
        """
        # The TelegramApprovalBot resolves management callbacks immediately.
        # For now, we rely on the _pending dict being cleared by the poller.
        # Future: pass a callback function to handle CLOSE_NOW, TRAIL_SL, etc.
        pass

    # ==================================================================
    # WEEKLY REPORT TRIGGER
    # ==================================================================

    def _maybe_trigger_weekly_report(self) -> None:
        """
        Trigger weekly report generation every Friday at 15:30 IST.
        Uses a sentinel file to ensure it fires only once per Friday.
        """
        if not _WEEKLY_REPORT_AVAILABLE:
            return

        now = datetime.now()
        if now.weekday() != 4:   # Only Friday (0=Mon, 4=Fri)
            return
        if not (now.hour == 15 and now.minute >= 30):
            return

        today_str = now.strftime("%Y-%m-%d")
        if today_str in self._weekly_report_fired:
            return

        sentinel = TRADE_LOG_DIR / "weekly" / f"weekly_report_sent_{today_str}.flag"
        if sentinel.exists():
            self._weekly_report_fired.add(today_str)
            return

        logger.info("[Engine] Triggering weekly report for %s", today_str)
        try:
            report_path = generate_weekly_report(ref_date=now)
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.touch()
            self._weekly_report_fired.add(today_str)
            if report_path:
                logger.info("[Engine] Weekly report generated: %s", report_path)
        except Exception as exc:
            logger.error("[Engine] Weekly report generation failed: %s", exc)

    # ==================================================================
    # HELPERS
    # ==================================================================

    def _set_engine_state(self, new_state: EngineState) -> None:
        """Thread-safe engine state update."""
        with self._engine_state_lock:
            old = self._engine_state
            self._engine_state = new_state
        logger.info("[Engine] State: %s → %s", old.value, new_state.value)

    def _generate_trade_id(self) -> str:
        """Generate a unique trade ID for this session."""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"{PAPER_SESSION_PREFIX}-{ts}"

    def _next_scan(self) -> int:
        """Increment and return the global scan counter."""
        self._scan_number += 1
        return self._scan_number

    def _interruptible_sleep(self, seconds: float) -> None:
        """
        Sleep for `seconds` but wake up immediately if stop_event is set.
        Checks every 1 second to allow fast shutdown.
        """
        deadline = time.time() + seconds
        while not self._stop_event.is_set() and time.time() < deadline:
            time.sleep(min(1.0, deadline - time.time()))


# ==================================================================
# MODULE-LEVEL ENTRY POINT
# ==================================================================

def run() -> None:
    """
    Module-level entry point called by main.py.

    Usage in main.py:
        from core.trading_engine import run
        run()
    """
    engine = TradingEngine()
    engine.start()


if __name__ == "__main__":
    run()
