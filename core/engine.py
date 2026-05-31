"""
core/engine.py
==============
TradingBot — Unified 3-Thread Engine.
Replaces core/trading_engine.py.

Architecture
------------
  Thread 1: signal_loop     — every SCAN_INTERVAL_SECONDS (5 min)
  Thread 2: tracker_loop    — every TRACKER_INTERVAL_SECONDS (30 sec)
  Thread 3: tg_poller       — every TELEGRAM_POLL_INTERVAL_SECONDS (3 sec)

Paper Trading Only — PAPER_TRADING_MODE must remain True.
"""

import logging
import sys
import threading
import time
from datetime import datetime
from typing import Optional

from config.settings import (
    PAPER_TRADING_MODE,
    SCAN_INTERVAL_SECONDS,
    TRACKER_INTERVAL_SECONDS,
    TELEGRAM_POLL_INTERVAL_SECONDS,
    HEARTBEAT_INTERVAL_SECONDS,
    TELEGRAM_APPROVAL_TIMEOUT_MINUTES,
    CONFIDENCE_MED_THRESHOLD,
)
from core.trade_manager import TradeManager, STATUS_OPEN, STATUS_PENDING
from core.data_engine import DataEngine
from core.signal_engine import SignalEngine
from core.market_hours import is_market_open as _mh_open
from core.risk_engine import RiskEngine
from analytics.logger import AnalyticsLogger
from telegram.bot import TelegramBot
from telegram.heartbeat import Heartbeat

logger = logging.getLogger(__name__)


# =============================================================================
# ENGINE
# =============================================================================

class Engine:
    """
    Orchestrates the three loops.

    Usage (called from main.py)
    ---------------------------
        engine = Engine()
        engine.run()    # blocks until Ctrl+C / stop.bat
    """

    def __init__(self) -> None:
        self._trade_mgr   = TradeManager()
        self._data        = DataEngine()
        self._signal_eng  = SignalEngine(self._data)
        self._risk        = RiskEngine()
        self._logger      = AnalyticsLogger()
        self._bot         = TelegramBot()
        self._heartbeat   = Heartbeat(self._bot)
        self._pending_since: Optional[datetime] = None
        self._stop        = threading.Event()
        self._start_time  = datetime.now()   # for /ping uptime
        logger.info("[Engine] Initialised | Paper=%s", PAPER_TRADING_MODE)

    # =========================================================================
    # PUBLIC ENTRY POINT
    # =========================================================================

    def run(self) -> None:
        """Start all three threads and block until stopped."""
        if not PAPER_TRADING_MODE:
            logger.critical("[Engine] PAPER_TRADING_MODE is False — refusing to start.")
            sys.exit(1)

        try:
            self._bot.send_startup()
        except Exception as exc:
            logger.warning("[Engine] Startup Telegram failed: %s", exc)

        threads = [
            threading.Thread(target=self._signal_loop,  name="SignalLoop",  daemon=True),
            threading.Thread(target=self._tracker_loop, name="TrackerLoop", daemon=True),
            threading.Thread(target=self._tg_poller,    name="TGPoller",    daemon=True),
        ]

        for t in threads:
            t.start()
            logger.info("[Engine] Started thread: %s", t.name)

        try:
            while not self._stop.is_set():
                time.sleep(1)
                for i, t in enumerate(threads):
                    if not t.is_alive():
                        logger.critical("[Engine] Thread %s died — restarting", t.name)
                        targets = [self._signal_loop, self._tracker_loop, self._tg_poller]
                        new_t = threading.Thread(target=targets[i], name=t.name, daemon=True)
                        new_t.start()
                        threads[i] = new_t
        except KeyboardInterrupt:
            logger.info("[Engine] KeyboardInterrupt — shutting down")
            self._stop.set()

        try:
            self._bot.send_shutdown()
        except Exception:
            pass

        logger.info("[Engine] Stopped.")
        sys.exit(0)

    # =========================================================================
    # THREAD 1 — SIGNAL LOOP
    # =========================================================================

    def _signal_loop(self) -> None:
        logger.info("[SignalLoop] Started")
        while not self._stop.is_set():
            try:
                self._run_signal_cycle()
            except Exception as exc:
                logger.error("[SignalLoop] Error: %s", exc, exc_info=True)
            for _ in range(SCAN_INTERVAL_SECONDS):
                if self._stop.is_set():
                    break
                time.sleep(1)
        logger.info("[SignalLoop] Stopped")

    def _run_signal_cycle(self) -> None:
        """One signal computation cycle."""
        # Gate 1: Market hours
        if not _mh_open():
            logger.debug("[SignalLoop] Market closed")
            return

        # Gate 2: Active trade already running
        if self._trade_mgr.has_active_trade():
            logger.debug("[SignalLoop] Active trade exists — skipping")
            return

        # Gate 3: Pre-signal risk check (no direction yet — checks limits/cooldown/loss)
        pre_check = self._risk.check_trade_allowed()
        if not pre_check.get("allowed", True):
            logger.info("[SignalLoop] Risk gate blocked: %s", pre_check.get("reason", ""))
            return

        # Compute signal
        logger.info("[SignalLoop] Scanning for signal...")
        signal = self._signal_eng.compute()
        self._logger.log_decision(signal)

        if not signal.get("is_trade"):
            logger.info("[SignalLoop] No trade | dir=%s score=%d",
                        signal.get("direction"), signal.get("adjusted_score", 0))
            return

        score = signal.get("adjusted_score", 0)
        if score < CONFIDENCE_MED_THRESHOLD:
            logger.info("[SignalLoop] Score %d < threshold — skipped", score)
            return

        # Gate 4: Post-signal risk check with direction and score (smart cooldown override)
        direction = signal.get("direction", "")
        post_check = self._risk.check_trade_allowed(
            signal_direction=direction, adjusted_score=score)
        if not post_check.get("allowed", True):
            logger.info("[SignalLoop] Risk gate (post-signal) blocked: %s",
                        post_check.get("reason", ""))
            return

        logger.info("[SignalLoop] SIGNAL | %s | score=%d | %s",
                    signal.get("trade_signal"), score, signal.get("confidence_level"))

        lots = 1
        try:
            msg_id = self._bot.send_signal_request(signal, lots)
        except Exception as exc:
            logger.error("[SignalLoop] Telegram send failed: %s", exc)
            return

        self._trade_mgr.open_pending(signal, lots, tg_message_id=msg_id)
        self._pending_since = datetime.now()
        logger.info("[SignalLoop] PENDING trade opened | lots=%d", lots)

    # =========================================================================
    # THREAD 2 — TRACKER LOOP
    # =========================================================================

    def _tracker_loop(self) -> None:
        logger.info("[TrackerLoop] Started")
        last_heartbeat = datetime.now()

        while not self._stop.is_set():
            try:
                self._run_tracker_cycle()
                now = datetime.now()
                if (now - last_heartbeat).total_seconds() >= HEARTBEAT_INTERVAL_SECONDS:
                    self._heartbeat.send(self._trade_mgr.get_trade())
                    last_heartbeat = now
            except Exception as exc:
                logger.error("[TrackerLoop] Error: %s", exc, exc_info=True)
            for _ in range(TRACKER_INTERVAL_SECONDS):
                if self._stop.is_set():
                    break
                time.sleep(1)
        logger.info("[TrackerLoop] Stopped")

    def _run_tracker_cycle(self) -> None:
        """One tracker evaluation cycle."""
        # Pending timeout
        if self._trade_mgr.is_pending() and self._pending_since:
            elapsed = (datetime.now() - self._pending_since).total_seconds()
            if elapsed > TELEGRAM_APPROVAL_TIMEOUT_MINUTES * 60:
                logger.info("[TrackerLoop] PENDING timed out — aborting")
                self._trade_mgr.abort_pending()
                self._pending_since = None
                try:
                    self._bot.send_text("Timed out — no approval received.")
                except Exception:
                    pass
                return

        if not self._trade_mgr.is_open():
            return

        # Get live price
        try:
            current_price = self._data.get_live_price()
            if current_price is None:
                logger.warning("[TrackerLoop] No live price — skipping")
                return
        except Exception as exc:
            logger.warning("[TrackerLoop] Live price error: %s", exc)
            return

        # Get TF data for reversal detection
        try:
            analysis = self._data.get_analysis()
            tf_data  = analysis.get("timeframe_data", {})
        except Exception as exc:
            logger.warning("[TrackerLoop] MTF error: %s", exc)
            tf_data = None

        result = self._trade_mgr.update(current_price, tf_data)
        action = result.get("action", "OK")

        if action == "OK":
            logger.info("[TrackerLoop] %s", result.get("message", ""))
            return

        trade = self._trade_mgr.get_trade()

        if action == "TARGET_HIT":
            n = result.get("target_n", 0)
            logger.info("[TrackerLoop] T%d HIT @ %.2f", n, current_price)
            try:
                self._bot.send_target_hit(trade, n, current_price)
            except Exception as exc:
                logger.error("[TrackerLoop] Telegram error: %s", exc)
            return

        # Closed: SL / reversal / EOD
        logger.info("[TrackerLoop] Trade CLOSED | action=%s", action)
        try:
            self._bot.send_trade_close(trade)
        except Exception as exc:
            logger.error("[TrackerLoop] Telegram close error: %s", exc)

        try:
            self._logger.log_trade(trade)
        except Exception as exc:
            logger.error("[TrackerLoop] Analytics error: %s", exc)

        try:
            outcome = "SL HIT" if "SL_HIT" in trade.close_reason else (
                "EOD CLOSE" if "EOD" in trade.close_reason else "TARGET HIT")
            self._risk.record_trade_closed(
                outcome=outcome,
                points_result=trade.pnl_points,
                direction=trade.direction,
            )
        except Exception as exc:
            logger.error("[TrackerLoop] Risk update error: %s", exc)

        self._trade_mgr.reset()
        self._pending_since = None

    # =========================================================================
    # THREAD 3 — TELEGRAM POLLER
    # =========================================================================

    def _tg_poller(self) -> None:
        logger.info("[TGPoller] Started")
        last_update_id: Optional[int] = None

        while not self._stop.is_set():
            try:
                updates = self._bot.get_updates(offset=last_update_id)
                for upd in updates:
                    last_update_id = upd["update_id"] + 1

                    # ── Branch 1: inline keyboard callbacks (APPROVE/REJECT/SCALE) ──
                    callback = upd.get("callback_query", {})
                    if callback:
                        action = callback.get("data", "").upper()
                        if action not in ("APPROVE", "REJECT", "SCALE"):
                            continue

                        logger.info("[TGPoller] Callback: %s", action)

                        if self._trade_mgr.is_pending():
                            result = self._trade_mgr.handle_approval(action)
                            if result == "APPROVED":
                                trade = self._trade_mgr.get_trade()
                                try:
                                    self._bot.send_trade_open(trade)
                                except Exception as exc:
                                    logger.error("[TGPoller] Trade open msg error: %s", exc)
                            elif result == "REJECTED":
                                try:
                                    self._bot.send_text("Trade rejected.")
                                except Exception:
                                    pass
                            try:
                                self._bot.answer_callback(
                                    callback.get("id", ""), f"Trade {result.lower()}")
                            except Exception:
                                pass
                        else:
                            try:
                                self._bot.answer_callback(
                                    callback.get("id", ""), "No pending trade")
                            except Exception:
                                pass
                        continue

                    # ── Branch 2: text commands (/ping /status /pnl) ──────────────
                    message = upd.get("message", {})
                    if not message:
                        continue
                    text = message.get("text", "").strip().lower().split()[0] if message.get("text") else ""
                    if not text.startswith("/"):
                        continue

                    logger.info("[TGPoller] Command: %s", text)

                    if text == "/ping":
                        self._handle_cmd_ping()
                    elif text == "/status":
                        self._handle_cmd_status()
                    elif text == "/pnl":
                        self._handle_cmd_pnl()

            except Exception as exc:
                logger.debug("[TGPoller] Poll error: %s", exc)

            for _ in range(TELEGRAM_POLL_INTERVAL_SECONDS):
                if self._stop.is_set():
                    break
                time.sleep(1)

        logger.info("[TGPoller] Stopped")

    # =========================================================================
    # COMMAND HANDLERS
    # =========================================================================

    def _handle_cmd_ping(self) -> None:
        """Handle /ping — reply with live NIFTY price + uptime."""
        try:
            price = self._data.get_live_price()
        except Exception:
            price = None

        elapsed   = datetime.now() - self._start_time
        hours, r  = divmod(int(elapsed.total_seconds()), 3600)
        mins      = r // 60
        uptime    = f"{hours}h {mins}m"

        try:
            self._bot.send_ping_reply(live_price=price, uptime_str=uptime)
        except Exception as exc:
            logger.error("[TGPoller] /ping reply failed: %s", exc)

    def _handle_cmd_status(self) -> None:
        """Handle /status — reply with risk counters + active trade state."""
        try:
            risk_state = dict(self._risk.state)
            risk_state["max_trades_today"] = self._risk.state.get(
                "max_trades_today",
                __import__("config.settings", fromlist=["MAX_TRADES_PER_DAY"]).MAX_TRADES_PER_DAY,
            )
            trade = self._trade_mgr.get_trade()
            self._bot.send_status_reply(risk_state=risk_state, trade=trade)
        except Exception as exc:
            logger.error("[TGPoller] /status reply failed: %s", exc)

    def _handle_cmd_pnl(self) -> None:
        """Handle /pnl — reply with today's closed trades summary."""
        try:
            from analytics.logger import AnalyticsLogger as AL
            today_trades = AL().load_today_trades()
            self._bot.send_pnl_reply(trades_today=today_trades)
        except Exception as exc:
            logger.error("[TGPoller] /pnl reply failed: %s", exc)
            try:
                self._bot.send_text("⚠️ Could not load today's trades.")
            except Exception:
                pass


# =============================================================================
# ENTRY POINT
# =============================================================================

def run() -> None:
    """Called by main.py — blocks until stopped."""
    engine = Engine()
    engine.run()
).strip().lower().split()[0] if message.get("text") else ""
                    if not text.startswith("/"):
                        continue

                    logger.info("[TGPoller] Command: %s", text)

                    if text == "/ping":
                        self._handle_cmd_ping()
                    elif text == "/status":
                        self._handle_cmd_status()
                    elif text == "/pnl":
                        self._handle_cmd_pnl()

            except Exception as exc:
                logger.debug("[TGPoller] Poll error: %s", exc)

            for _ in range(TELEGRAM_POLL_INTERVAL_SECONDS):
                if self._stop.is_set():
                    break
                time.sleep(1)

        logger.info("[TGPoller] Stopped")

    # =========================================================================
    # COMMAND HANDLERS
    # =========================================================================

    def _handle_cmd_ping(self):
        """Handle /ping -- reply with live NIFTY price + uptime."""
        try:
            price = self._data.get_live_price()
        except Exception:
            price = None

        elapsed  = datetime.now() - self._start_time
        hours, r = divmod(int(elapsed.total_seconds()), 3600)
        mins     = r // 60
        uptime   = f"{hours}h {mins}m"
        try:
            self._bot.send_ping_reply(live_price=price, uptime_str=uptime)
        except Exception as exc:
            logger.error("[TGPoller] /ping reply failed: %s", exc)

    def _handle_cmd_status(self):
        """Handle /status -- reply with risk counters + active trade state."""
        try:
            from config.settings import MAX_TRADES_PER_DAY
            risk_state = dict(self._risk.state)
            risk_state.setdefault("max_trades_today", MAX_TRADES_PER_DAY)
            trade = self._trade_mgr.get_trade()
            self._bot.send_status_reply(risk_state=risk_state, trade=trade)
        except Exception as exc:
            logger.error("[TGPoller] /status reply failed: %s", exc)

    def _handle_cmd_pnl(self):
        """Handle /pnl -- reply with today closed trades summary."""
        try:
            from analytics.logger import AnalyticsLogger as AL
            today_trades = AL().load_today_trades()
            self._bot.send_pnl_reply(trades_today=today_trades)
        except Exception as exc:
            logger.error("[TGPoller] /pnl reply failed: %s", exc)
            try:
                self._bot.send_text("Could not load today trades.")
            except Exception:
                pass


# =============================================================================
# ENTRY POINT
# =============================================================================

def run():
    """Called by main.py -- blocks until stopped."""
    engine = Engine()
    engine.run()
