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

import json
import logging
import os
import re
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
    DATA_DIR,
    NIFTY_LOT_SIZE,
    OPTION_DELTA,
    ACCOUNT_CAPITAL,
    TRADE_EXTENSION_BATCH,
    TRADE_EXTENSION_MIN_SCORE,
)
from core.data_engine import DataEngine
from core.signal_engine import SignalEngine, CONFIDENCE_LOW, CONFIDENCE_VERY_HIGH
from core.telegram_approval_bot import TelegramApprovalBot, CallbackOutcome
from core.trade_state import TradeStateManager
from core.risk_engine import RiskEngine
from core.oi_analysis import OIAnalysis
from core.market_hours import is_market_open, is_eod_close_time, seconds_until_next_open
from analytics.decision_logger import DecisionLogger
from analytics.trade_archiver import TradeArchiver

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
        self._oi        = OIAnalysis()          # For option premium fetching at entry
        self._archiver  = TradeArchiver()        # Daily trade archiver

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

        # ---- Archive sentinel (prevents double-archive on same day) ----
        self._archive_date_fired: str = ""

        # ---- Scan counter (for DecisionLogger) ----
        self._scan_number: int = 0

        # ---- Live status: last scan snapshot (for dashboard) ----
        self._last_scan_info: dict = {}
        self._scan_history: list   = []   # Rolling last 20 scans

        # ---- Closed trades today (for dashboard history) ----
        self._closed_trades_today: list = []
        self._load_closed_trades()          # Reload from disk on startup (survives restart)

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

        # ---- Snapshot this scan for the live dashboard ----
        _snap = {
            "scan_number":  self._scan_number,
            "time":         datetime.now().strftime("%H:%M:%S"),
            "price":        sig.get("price"),
            "direction":    sig.get("direction", "?"),
            "base_score":   sig.get("base_score", 0),
            "adj_score":    sig.get("adjusted_score", 0),
            "confidence":   sig.get("confidence_level", "?"),
            "decision":     "PENDING",
            "reason":       "",
            "trade_id":     None,
        }

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
            _snap.update({"decision": "SIDEWAYS", "reason": reason})
            self._push_scan_snap(_snap)
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
            _snap.update({"decision": "LOW CONFIDENCE", "reason": reason})
            self._push_scan_snap(_snap)
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
            signal_direction = sig["direction"],
            adjusted_score   = adjusted_score,
        )

        if not risk_check.get("allowed", False):
            reason = risk_check.get("reason", "Risk engine blocked trade")

            # ---- Trade limit extension: ask Telegram if HIGH confidence ----
            if (risk_check.get("can_extend", False)
                    and adjusted_score >= TRADE_EXTENSION_MIN_SCORE):
                logger.info(
                    "[SignalLoop] At daily trade limit but score=%d >= threshold — "
                    "requesting Telegram extension approval", adjusted_score
                )
                extension_approved = self._tgbot.send_extension_request(
                    sig, extension_batch=TRADE_EXTENSION_BATCH
                )
                if extension_approved:
                    new_limit = self._risk.grant_trade_extension(TRADE_EXTENSION_BATCH)
                    logger.info("[SignalLoop] Extension approved — new limit: %d/day", new_limit)
                    self._tgbot.send_trade_update(
                        f"✅ <b>Trade limit extended!</b>\n"
                        f"New daily limit: <b>{new_limit} trades</b>\n"
                        f"Continuing with signal..."
                    )
                    # Re-run risk check with the new limit
                    self._risk.reload()
                    risk_check = self._risk.check_trade_allowed(
                        signal_direction=sig["direction"],
                        adjusted_score=adjusted_score,
                    )
                    if risk_check.get("allowed", False):
                        # Fall through — proceed to trade approval below
                        pass
                    else:
                        reason = risk_check.get("reason", "Still blocked after extension")
                        logger.info("[SignalLoop] Still blocked after extension: %s", reason)
                        _snap.update({"decision": "BLOCKED", "reason": reason})
                        self._push_scan_snap(_snap)
                        self._set_engine_state(EngineState.IDLE)
                        return
                else:
                    reason = "Trade limit extension declined by user"
                    logger.info("[SignalLoop] Extension declined — %s", reason)
                    _snap.update({"decision": "BLOCKED", "reason": reason})
                    self._push_scan_snap(_snap)
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
            else:
                # Not extendable (consecutive loss lockout, daily loss limit, etc.)
                logger.info("[SignalLoop] Risk blocked (not extendable): %s", reason)
                _snap.update({"decision": "BLOCKED", "reason": reason})
                self._push_scan_snap(_snap)
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
            _snap.update({"decision": "APPROVED", "reason": f"Telegram {outcome.value}", "trade_id": trade_id})
            self._push_scan_snap(_snap)
            self._open_trade(sig, trade_id, scale_lots)

        else:
            # REJECTED or EXPIRED
            reason = f"Telegram {outcome.value} by user"
            logger.info("[SignalLoop] Trade not opened — %s", reason)
            _snap.update({"decision": outcome.value, "reason": reason, "trade_id": trade_id})
            self._push_scan_snap(_snap)
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

        # ---- Fetch option premium from NSE (best-effort, fallback to estimate) ----
        _signal_str = sig.get("trade_signal", "")
        _strike_match = re.search(r'\b(\d{4,6})\b', _signal_str)
        _strike     = int(_strike_match.group(1)) if _strike_match else None
        _direction  = sig.get("direction", "CE")   # "CE" or "PE"
        _opt_dir    = "CE" if _direction in ("CE", "BULLISH") else "PE"

        option_premium   = None
        premium_source   = "none"
        if _strike:
            try:
                option_premium = self._oi.get_option_premium(_strike, _opt_dir)
                premium_source = "nse_api" if option_premium else "none"
            except Exception:
                pass

        # Fallback estimate: ATM NIFTY weekly option ~150 pts (reasonable approximation)
        if option_premium is None:
            option_premium = 150.0
            premium_source = "estimated"
            logger.info("[Engine] Option premium estimated at %.0f pts (NSE fetch unavailable)", option_premium)

        sig["option_premium"]  = option_premium
        sig["premium_source"]  = premium_source
        sig["capital_invested"] = round(option_premium * NIFTY_LOT_SIZE * lots, 2)

        # ---- Open in state machine ----
        try:
            actual_trade_id = self._state.open_trade(
                signal           = sig["trade_signal"],
                entry_price      = price,
                trend            = sig["trend"],
                vwap             = vwap,
                ema9             = ema9,
                lots             = lots,
                option_premium   = option_premium,
                premium_source   = premium_source,
                capital_invested = sig["capital_invested"],
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

        # ---- INR risk/reward for confirmation message ----
        # Note: NIFTY_LOT_SIZE, OPTION_DELTA, ACCOUNT_CAPITAL already imported at module level
        _pts_per_lot = NIFTY_LOT_SIZE * OPTION_DELTA
        _sl_inr      = STOP_LOSS_POINTS  * _pts_per_lot * lots
        _t1_inr      = TARGET_1_POINTS   * _pts_per_lot * lots
        _t2_inr      = TARGET_2_POINTS   * _pts_per_lot * lots
        _t3_inr      = TARGET_3_POINTS   * _pts_per_lot * lots
        _risk_pct    = round((_sl_inr / ACCOUNT_CAPITAL) * 100, 1)

        # ---- Telegram confirmation ----
        lots_str = f" (x{lots} lots)" if lots > 1 else ""
        self._tgbot.send_trade_update(
            f"✅ <b>TRADE OPENED{lots_str}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Signal:</b> {sig['trade_signal']}\n"
            f"<b>Entry:</b> ₹{price:,.2f}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 <b>Capital at Risk:</b> ₹{_sl_inr:,.0f} ({_risk_pct}% of ₹{ACCOUNT_CAPITAL:,.0f})\n"
            f"🛑 <b>SL:</b> {STOP_LOSS_POINTS} pts → -₹{_sl_inr:,.0f}\n"
            f"🎯 <b>T1:</b> {TARGET_1_POINTS} pts → +₹{_t1_inr:,.0f}\n"
            f"🎯 <b>T2:</b> {TARGET_2_POINTS} pts → +₹{_t2_inr:,.0f}\n"
            f"🎯 <b>T3:</b> {TARGET_3_POINTS} pts → +₹{_t3_inr:,.0f}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
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

        # ---- Reload state; write dashboard JSON even when idle ----
        self._state.reload()
        if not self._state.has_active_trade():
            self._write_live_status(trade=None, current_price=None, pts_pnl=None)
            self._maybe_archive_eod()   # Still archive at EOD even if no trade is open
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

        # ---- Write live status JSON for dashboard (every tracker cycle) ----
        self._write_live_status(trade=trade, current_price=current_price, pts_pnl=pts_pnl)

        # ---- EOD full archive (15:30+ sentinel — fires once per day) ----
        self._maybe_archive_eod()

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

        # ---- Record closed trade for dashboard history ----
        _lots    = trade.get("lots", 1)
        _pts_per = NIFTY_LOT_SIZE * OPTION_DELTA
        _inr_pnl = round(pts_pnl * _lots * _pts_per, 2)
        _opt_prem      = trade.get("option_premium", 150.0) or 150.0
        _cap_invested  = trade.get("capital_invested") or round(_opt_prem * NIFTY_LOT_SIZE * _lots, 2)
        closed_record = {
            "trade_id":        trade_id,
            "signal":          trade.get("signal", "?"),
            "direction":       trade.get("direction", "?"),
            "lots":            _lots,
            "entry_price":     trade.get("entry_price", 0),
            "exit_price":      round(current_price, 2),
            "entry_time":      trade.get("entry_time", ""),
            "exit_time":       datetime.now().strftime("%H:%M:%S"),
            "pts_pnl":         round(pts_pnl, 2),
            "inr_pnl":         _inr_pnl,
            "option_premium":  round(_opt_prem, 2),
            "capital_invested": _cap_invested,
            "outcome":         outcome,
            "milestones":      trade.get("milestones_hit", []),
        }
        self._closed_trades_today.append(closed_record)
        self._save_closed_trades()

        # ---- Archive this day's data (runs after every close, safe to call multiple times) ----
        try:
            self._risk.reload()
            self._archiver.archive_day(
                trades     = self._closed_trades_today,
                risk_state = self._risk.state if hasattr(self._risk, "state") else {},
            )
        except Exception as _arc_exc:
            logger.warning("[Engine] Archive update after trade close failed: %s", _arc_exc)

        # ---- Telegram notification ----
        emoji    = "✅" if pts_pnl > 0 else ("❌" if pts_pnl < 0 else "⚪")
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

    def _maybe_archive_eod(self) -> None:
        """
        Fire the daily archive once per day at or after 15:30 IST.
        Uses a date-string sentinel so it only runs once even across
        multiple tracker cycles after market close.
        """
        now = datetime.now()
        # Only trigger at/after 15:30 on any weekday
        if now.hour < 15 or (now.hour == 15 and now.minute < 30):
            return
        today_str = now.strftime("%Y-%m-%d")
        if self._archive_date_fired == today_str:
            return   # Already archived today

        self._archive_date_fired = today_str
        try:
            self._risk.reload()
            archive_path = self._archiver.archive_day(
                trades     = self._closed_trades_today,
                risk_state = self._risk.state if hasattr(self._risk, "state") else {},
                date       = today_str,
            )
            logger.info("[Engine] EOD archive written: %s (%d trades)", archive_path, len(self._closed_trades_today))
        except Exception as exc:
            logger.warning("[Engine] EOD archive failed: %s", exc)
            # Reset sentinel so it can retry next cycle
            self._archive_date_fired = ""

    def _load_closed_trades(self) -> None:
        """
        Load today's closed trades from data/closed_trades_today.json on startup.
        Resets if the date has changed (new trading day).
        """
        path = DATA_DIR / "closed_trades_today.json"
        try:
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                today = datetime.now().strftime("%Y-%m-%d")
                if saved.get("date") == today:
                    self._closed_trades_today = saved.get("trades", [])
                    logger.info(
                        "[Engine] Loaded %d closed trades from disk (date=%s)",
                        len(self._closed_trades_today), today,
                    )
                else:
                    logger.info("[Engine] closed_trades_today.json is from a previous day — resetting")
        except Exception as exc:
            logger.warning("[Engine] Could not load closed trades: %s", exc)

    def _save_closed_trades(self) -> None:
        """Atomically persist today's closed trades to data/closed_trades_today.json."""
        try:
            today    = datetime.now().strftime("%Y-%m-%d")
            payload  = {"date": today, "trades": self._closed_trades_today}
            out_path = DATA_DIR / "closed_trades_today.json"
            tmp_path = DATA_DIR / "closed_trades_today.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, default=str)
            os.replace(tmp_path, out_path)
        except Exception as exc:
            logger.warning("[Engine] Could not save closed trades: %s", exc)

    def _push_scan_snap(self, snap: dict) -> None:
        """
        Add a scan snapshot to the rolling history (max 20 entries).
        Called from _signal_cycle() after each scan decision.
        """
        self._last_scan_info = snap.copy()
        self._scan_history.append(snap.copy())
        if len(self._scan_history) > 20:
            self._scan_history = self._scan_history[-20:]

    def _write_live_status(
        self,
        trade: Optional[dict],
        current_price: Optional[float],
        pts_pnl: Optional[float],
    ) -> None:
        """
        Write data/live_status.json atomically every tracker cycle.
        This file is consumed by the live dashboard (dashboard_server.py).

        Schema:
          bot_status        — RUNNING / IDLE / PAPER_TRADING
          engine_state      — IDLE / PENDING_APPROVAL / TRADE_ACTIVE
          last_updated      — ISO timestamp
          current_price     — live NIFTY index price
          active_trade      — null or full trade dict + computed fields
          daily             — trades_today, daily_pnl_points, daily_pnl_inr, consecutive_losses
          scan_history      — last 20 scans (time, decision, score, direction)
          config            — sl_pts, t1/2/3_pts, lot_size, capital
        """
        try:
            # ---- Daily risk state ----
            self._risk.reload()
            risk_state          = self._risk.state if hasattr(self._risk, "state") else {}
            trades_today        = risk_state.get("trades_today", 0)
            daily_pnl_pts       = risk_state.get("daily_pnl_points", 0.0)
            consecutive_losses  = risk_state.get("consecutive_losses", 0)
            consecutive_wins    = risk_state.get("consecutive_wins", 0)
            extension           = risk_state.get("trade_limit_extension", 0)
            _pts_per_lot        = NIFTY_LOT_SIZE * OPTION_DELTA
            realized_pnl_inr    = round(daily_pnl_pts * _pts_per_lot, 2)

            # ---- Active trade block ----
            active_trade_block = None
            if trade and current_price is not None and pts_pnl is not None:
                lots        = trade.get("lots", 1)
                entry_price = trade.get("entry_price", 0)
                direction   = trade.get("direction", "CE")
                signal_str  = trade.get("signal", "")
                milestones  = trade.get("milestones_hit", [])

                # Parse strike from signal string e.g. "BUY 23800 CE"
                strike = None
                m = re.search(r'\b(\d{4,6})\b', signal_str)
                if m:
                    strike = int(m.group(1))

                # Absolute SL / target levels (NIFTY price)
                if direction == "CE":
                    sl_level = entry_price - STOP_LOSS_POINTS
                    t1_level = entry_price + TARGET_1_POINTS
                    t2_level = entry_price + TARGET_2_POINTS
                    t3_level = entry_price + TARGET_3_POINTS
                else:
                    sl_level = entry_price + STOP_LOSS_POINTS
                    t1_level = entry_price - TARGET_1_POINTS
                    t2_level = entry_price - TARGET_2_POINTS
                    t3_level = entry_price - TARGET_3_POINTS

                # Entry time + duration
                entry_time_str = trade.get("entry_time", "")
                duration_mins  = None
                try:
                    entry_dt   = datetime.strptime(
                        f"{trade.get('entry_date', datetime.now().strftime('%Y-%m-%d'))} {entry_time_str}",
                        "%Y-%m-%d %H:%M:%S"
                    )
                    duration_mins = int((datetime.now() - entry_dt).total_seconds() / 60)
                except Exception:
                    pass

                inr_pnl = pts_pnl * lots * _pts_per_lot

                # Option premium + capital invested
                opt_premium      = trade.get("option_premium", 150.0) or 150.0
                premium_source   = trade.get("premium_source", "estimated")
                capital_invested = trade.get("capital_invested") or round(opt_premium * NIFTY_LOT_SIZE * lots, 2)
                current_value    = round(capital_invested + inr_pnl, 2)

                active_trade_block = {
                    "trade_id":        trade.get("trade_id"),
                    "signal":          signal_str,
                    "direction":       direction,
                    "strike":          strike,
                    "lots":            lots,
                    "entry_price":     round(entry_price, 2),
                    "entry_time":      entry_time_str,
                    "current_price":   round(current_price, 2),
                    "pts_pnl":         round(pts_pnl, 2),
                    "inr_pnl":         round(inr_pnl, 2),
                    # Option / capital details
                    "option_premium":  round(opt_premium, 2),
                    "premium_source":  premium_source,
                    "capital_invested": capital_invested,    # Total premium paid (₹)
                    "current_value":   current_value,        # Current position value (₹)
                    "money_on_hold":   capital_invested,     # Locked until trade closes
                    # Levels
                    "sl_level":        round(sl_level, 2),
                    "t1_level":        round(t1_level, 2),
                    "t2_level":        round(t2_level, 2),
                    "t3_level":        round(t3_level, 2),
                    "sl_pts":          STOP_LOSS_POINTS,
                    "t1_pts":          TARGET_1_POINTS,
                    "t2_pts":          TARGET_2_POINTS,
                    "t3_pts":          TARGET_3_POINTS,
                    "sl_inr":          round(STOP_LOSS_POINTS * _pts_per_lot * lots, 2),
                    "t1_inr":          round(TARGET_1_POINTS  * _pts_per_lot * lots, 2),
                    "t2_inr":          round(TARGET_2_POINTS  * _pts_per_lot * lots, 2),
                    "t3_inr":          round(TARGET_3_POINTS  * _pts_per_lot * lots, 2),
                    "milestones":      milestones,
                    "duration_mins":   duration_mins,
                    "status":          trade.get("status", "OPEN"),
                    "trend":           trade.get("trend", ""),
                    "vwap_at_entry":   trade.get("vwap_at_entry"),
                    "ema9_at_entry":   trade.get("ema9_at_entry"),
                }

            # ---- Engine state string ----
            with self._engine_state_lock:
                eng_state = self._engine_state.value

            # ---- Build payload ----
            payload = {
                "bot_status":    "PAPER TRADING" if PAPER_TRADING_MODE else "LIVE TRADING",
                "engine_state":  eng_state,
                "last_updated":  datetime.now().isoformat(timespec="seconds"),
                "current_price": round(current_price, 2) if current_price else None,
                "active_trade":  active_trade_block,
                "daily": {
                    "trades_today":           trades_today,
                    "trades_won":             risk_state.get("trades_won", 0),
                    "trades_lost":            risk_state.get("trades_lost", 0),
                    "daily_pnl_points":       round(daily_pnl_pts, 2),
                    "daily_pnl_inr":          realized_pnl_inr,
                    "gross_profit_points":    risk_state.get("gross_profit_points", 0.0),
                    "gross_loss_points":      risk_state.get("gross_loss_points", 0.0),
                    "win_rate":               round(risk_state.get("trades_won", 0) / max(trades_today, 1) * 100, 1),
                    "consecutive_losses":     consecutive_losses,
                    "consecutive_wins":       consecutive_wins,
                    "max_consecutive_wins":   risk_state.get("max_consecutive_wins", 0),
                    "max_consecutive_losses": risk_state.get("max_consecutive_losses", 0),
                    "max_trades":             5 + extension,
                    "max_trades_base":        5,
                    "trade_limit_extension":  extension,
                    "capital":                ACCOUNT_CAPITAL,
                },
                # Balance snapshot
                "balance": {
                    "starting_capital":   ACCOUNT_CAPITAL,
                    "realized_pnl_inr":   realized_pnl_inr,
                    "unrealized_pnl_inr": round(active_trade_block["inr_pnl"], 2) if active_trade_block else 0.0,
                    "money_on_hold":      active_trade_block["money_on_hold"] if active_trade_block else 0.0,
                    "net_balance":        round(ACCOUNT_CAPITAL + realized_pnl_inr + (active_trade_block["inr_pnl"] if active_trade_block else 0.0), 2),
                    "available_cash":     round(ACCOUNT_CAPITAL + realized_pnl_inr - (active_trade_block["money_on_hold"] if active_trade_block else 0.0), 2),
                },
                "closed_trades": list(reversed(self._closed_trades_today)),  # newest first
                "config": {
                    "sl_pts":   STOP_LOSS_POINTS,
                    "t1_pts":   TARGET_1_POINTS,
                    "t2_pts":   TARGET_2_POINTS,
                    "t3_pts":   TARGET_3_POINTS,
                    "lot_size": NIFTY_LOT_SIZE,
                    "delta":    OPTION_DELTA,
                    "capital":  ACCOUNT_CAPITAL,
                },
                "scan_history": list(reversed(self._scan_history)),  # newest first
                "last_scan":    self._last_scan_info,
            }

            # ---- Atomic write (tmp → replace) ----
            out_path  = DATA_DIR / "live_status.json"
            tmp_path  = DATA_DIR / "live_status.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, default=str)
            os.replace(tmp_path, out_path)

        except Exception as exc:
            logger.warning("[Engine] live_status.json write failed: %s", exc)

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
