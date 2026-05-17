"""
core/risk_engine.py
===================
Position sizing and daily risk management engine for the TradingBot system.

Responsibilities:
    - Calculate quantity (lots) dynamically based on capital and SL distance
    - Enforce per-trade risk limits (max % of capital)
    - Track and enforce daily limits: max trades, max loss, cooldowns, consecutive losses
    - Persist daily state across process restarts (resets each new trading day)
    - Provide rejection reasons for blocked trades

Persistence:
    - data/daily_risk_state.json (atomic writes, auto-resets on date change)
    - Both assistant and tracker share this file; call reload() before each use

Position sizing formula:
    max_risk_INR      = ACCOUNT_CAPITAL * MAX_RISK_PCT / 100
    sl_loss_per_lot   = STOP_LOSS_POINTS * OPTION_DELTA * NIFTY_LOT_SIZE
    suggested_lots    = floor(max_risk_INR / sl_loss_per_lot)

Example (defaults):
    max_risk_INR    = 5000 * 20% = Rs.1000
    sl_loss_per_lot = 10 * 0.5 * 75 = Rs.375
    lots            = floor(1000 / 375) = 2
    max_loss        = 2 * 375 = Rs.750

Usage:
    from core.risk_engine import RiskEngine

    risk = RiskEngine()
    risk.reload()

    check = risk.check_trade_allowed()
    if not check["allowed"]:
        print(check["reason"])
        return

    sizing = risk.calculate_position_size()
    # sizing = {"lots": 2, "max_loss_inr": 750.0, "risk_pct": 15.0}

    risk.record_trade_opened()   # call after open_trade() succeeds
    risk.record_trade_closed("SL HIT", -10.0)  # call after close_trade()
"""

import json
import logging
import math
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from config.config import (
    DAILY_RISK_STATE_FILE,
    ACCOUNT_CAPITAL, MAX_RISK_PCT, MAX_DAILY_LOSS_PCT,
    MAX_TRADES_PER_DAY, COOLDOWN_AFTER_SL_MINUTES, MAX_CONSECUTIVE_LOSSES,
    NIFTY_LOT_SIZE, OPTION_DELTA, STOP_LOSS_POINTS,
)

# =========================
# LOGGING
# =========================

logger = logging.getLogger(__name__)

# =========================
# WIN / LOSS OUTCOME SETS
# =========================

_WIN_OUTCOMES  = {"TARGET 3 HIT", "TARGET 2 HIT", "TARGET 1 HIT"}
_LOSS_OUTCOMES = {"SL HIT"}
# EOD CLOSE: counted as loss only if points_result < 0


# =========================
# DEFAULT DAILY STATE
# =========================

def _empty_state(today: str) -> dict:
    return {
        "date":               today,
        "trades_today":       0,
        "daily_pnl_points":   0.0,
        "consecutive_losses": 0,
        "cooldown_until":     None,
        "last_updated":       None,
    }


# =========================
# RISK ENGINE
# =========================

class RiskEngine:
    """
    Manages daily risk limits and position sizing for the TradingBot.

    One instance per process. Call reload() at the top of each use
    so both processes see the current state.
    """

    def __init__(self, state_file: Path = DAILY_RISK_STATE_FILE) -> None:
        self._state_file = Path(state_file)
        self._tmp_file   = self._state_file.with_suffix(".tmp")
        self.state: dict = self._load()
        logger.info(
            "[RISK] RiskEngine initialized | "
            "date=" + self.state["date"] + " | "
            "trades=" + str(self.state["trades_today"]) + " | "
            "pnl=" + str(self.state["daily_pnl_points"]) + "pts | "
            "consec_losses=" + str(self.state["consecutive_losses"])
        )

    # =========================
    # PERSISTENCE
    # =========================

    def _load(self) -> dict:
        today = datetime.now().strftime("%Y-%m-%d")

        if not self._state_file.exists():
            logger.info("[RISK] No daily state file — starting fresh for " + today)
            return _empty_state(today)

        try:
            with open(self._state_file, "r", encoding="utf-8") as f:
                loaded = json.load(f)

            # Reset if date has changed (new trading day)
            if loaded.get("date") != today:
                logger.info(
                    "[RISK] New trading day (" + today + ") — resetting daily counters. "
                    "Previous: " + str(loaded.get("date"))
                )
                return _empty_state(today)

            # Merge with defaults for forward compatibility
            merged = _empty_state(today)
            merged.update(loaded)
            return merged

        except (json.JSONDecodeError, IOError) as exc:
            logger.error("[RISK] State file unreadable: " + str(exc) + " — resetting")
            return _empty_state(today)

    def _save(self) -> None:
        self.state["last_updated"] = datetime.now().isoformat()
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._tmp_file, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)
            os.replace(str(self._tmp_file), str(self._state_file))
            logger.debug("[RISK] State saved")
        except IOError as exc:
            logger.error("[RISK] Failed to save state: " + str(exc))

    def reload(self) -> None:
        """Re-read state from disk. Call before check_trade_allowed() or record_*()."""
        self.state = self._load()

    # =========================
    # POSITION SIZING
    # =========================

    def calculate_position_size(self) -> dict:
        """
        Calculate how many lots to trade given current capital and risk settings.

        Returns:
            dict with keys:
                lots        (int)   — suggested number of lots
                max_loss_inr (float) — estimated max loss if SL is hit
                risk_pct    (float) — actual risk % of capital
        """
        max_risk_inr    = ACCOUNT_CAPITAL * MAX_RISK_PCT / 100
        sl_loss_per_lot = STOP_LOSS_POINTS * OPTION_DELTA * NIFTY_LOT_SIZE

        if sl_loss_per_lot <= 0:
            lots = 1
        else:
            lots = max(1, int(math.floor(max_risk_inr / sl_loss_per_lot)))

        estimated_loss = lots * sl_loss_per_lot
        risk_pct       = round((estimated_loss / ACCOUNT_CAPITAL) * 100, 1) if ACCOUNT_CAPITAL > 0 else 0.0

        logger.debug(
            "[RISK] Sizing: lots=" + str(lots) +
            " | max_loss=Rs." + str(round(estimated_loss, 2)) +
            " | risk=" + str(risk_pct) + "%"
        )

        return {
            "lots":         lots,
            "max_loss_inr": round(estimated_loss, 2),
            "risk_pct":     risk_pct,
        }

    # =========================
    # TRADE VALIDATION
    # =========================

    def check_trade_allowed(self) -> dict:
        """
        Check all daily risk rules before allowing a new trade.

        Returns:
            {"allowed": True, "reason": ""}          — trade permitted
            {"allowed": False, "reason": "<why>"}    — trade blocked
        """
        now = datetime.now()

        # 1. Max trades per day
        if self.state["trades_today"] >= MAX_TRADES_PER_DAY:
            reason = (
                "Max trades/day reached: " + str(self.state["trades_today"]) +
                "/" + str(MAX_TRADES_PER_DAY)
            )
            logger.warning("[RISK] BLOCKED — " + reason)
            return {"allowed": False, "reason": reason}

        # 2. Max daily loss
        max_loss_inr    = ACCOUNT_CAPITAL * MAX_DAILY_LOSS_PCT / 100
        daily_loss_inr  = abs(self.state["daily_pnl_points"]) * OPTION_DELTA * NIFTY_LOT_SIZE
        if self.state["daily_pnl_points"] < 0 and daily_loss_inr >= max_loss_inr:
            reason = (
                "Daily loss limit hit: Rs." + str(round(daily_loss_inr, 0)) +
                " >= Rs." + str(round(max_loss_inr, 0)) +
                " (" + str(MAX_DAILY_LOSS_PCT) + "% of capital)"
            )
            logger.warning("[RISK] BLOCKED — " + reason)
            return {"allowed": False, "reason": reason}

        # 3. SL cooldown
        if self.state["cooldown_until"] is not None:
            try:
                cooldown_dt = datetime.fromisoformat(self.state["cooldown_until"])
                if now < cooldown_dt:
                    remaining = max(1, int((cooldown_dt - now).total_seconds() / 60))
                    reason = (
                        "SL cooldown active: " + str(remaining) + "m remaining. "
                        "Wait before re-entering."
                    )
                    logger.warning("[RISK] BLOCKED — " + reason)
                    return {"allowed": False, "reason": reason}
                else:
                    # Cooldown expired — clear it
                    self.state["cooldown_until"] = None
                    self._save()
            except ValueError:
                self.state["cooldown_until"] = None
                self._save()

        # 4. Consecutive loss lockout
        if self.state["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
            reason = (
                "Consecutive loss lockout: " + str(self.state["consecutive_losses"]) +
                " losses in a row (limit=" + str(MAX_CONSECUTIVE_LOSSES) + "). "
                "Review strategy before resuming."
            )
            logger.warning("[RISK] BLOCKED — " + reason)
            return {"allowed": False, "reason": reason}

        logger.debug("[RISK] Trade allowed — all checks passed")
        return {"allowed": True, "reason": ""}

    # =========================
    # RECORD EVENTS
    # =========================

    def record_trade_opened(self) -> None:
        """
        Increment trade count when a trade is successfully opened.
        Call this immediately after state.open_trade() succeeds.
        """
        self.state["trades_today"] = self.state.get("trades_today", 0) + 1
        self._save()
        logger.info(
            "[RISK] Trade opened. Today: " +
            str(self.state["trades_today"]) + "/" + str(MAX_TRADES_PER_DAY) + " trades"
        )

    def record_trade_closed(self, outcome: str, points_result: float) -> None:
        """
        Update daily P&L, consecutive loss counter, and cooldown after a trade closes.

        Args:
            outcome:       Human-readable outcome string (e.g. "SL HIT", "TARGET 3 HIT")
            points_result: NIFTY index points gained (+) or lost (-)
        """
        # Update daily P&L
        self.state["daily_pnl_points"] = round(
            self.state.get("daily_pnl_points", 0.0) + points_result, 2
        )

        # Determine win / loss
        is_loss = False
        if outcome in _LOSS_OUTCOMES:
            is_loss = True
        elif outcome == "EOD CLOSE" and points_result < 0:
            is_loss = True

        if is_loss:
            self.state["consecutive_losses"] = self.state.get("consecutive_losses", 0) + 1
            # Set cooldown (only for SL HIT, not EOD)
            if outcome == "SL HIT":
                cooldown_dt = datetime.now() + timedelta(minutes=COOLDOWN_AFTER_SL_MINUTES)
                self.state["cooldown_until"] = cooldown_dt.isoformat()
                logger.info(
                    "[RISK] SL hit — cooldown set until " +
                    cooldown_dt.strftime("%H:%M")
                )
        else:
            # Win or neutral — reset consecutive loss streak
            self.state["consecutive_losses"] = 0

        self._save()

        # Summary log
        daily_loss_inr = abs(self.state["daily_pnl_points"]) * OPTION_DELTA * NIFTY_LOT_SIZE
        logger.info(
            "[RISK] Trade closed | outcome=" + outcome +
            " | pts=" + ("+{:.2f}".format(points_result) if points_result >= 0 else "{:.2f}".format(points_result)) +
            " | daily_pnl=" + str(self.state["daily_pnl_points"]) + "pts" +
            " (~Rs." + str(round(daily_loss_inr if self.state["daily_pnl_points"] < 0 else 0, 0)) + " loss)" +
            " | consec_losses=" + str(self.state["consecutive_losses"])
        )

    # =========================
    # SUMMARY
    # =========================

    def get_summary(self) -> dict:
        """
        Return current daily risk state as a readable dict.
        Useful for logging and Telegram status messages.
        """
        daily_pnl_inr = self.state["daily_pnl_points"] * OPTION_DELTA * NIFTY_LOT_SIZE
        sizing        = self.calculate_position_size()
        return {
            "date":               self.state["date"],
            "trades_today":       self.state["trades_today"],
            "max_trades":         MAX_TRADES_PER_DAY,
            "daily_pnl_points":   self.state["daily_pnl_points"],
            "daily_pnl_inr":      round(daily_pnl_inr, 2),
            "consecutive_losses": self.state["consecutive_losses"],
            "cooldown_until":     self.state["cooldown_until"],
            "suggested_lots":     sizing["lots"],
            "max_loss_inr":       sizing["max_loss_inr"],
            "risk_pct":           sizing["risk_pct"],
        }

    def __repr__(self) -> str:
        return (
            "RiskEngine(date=" + self.state["date"] +
            ", trades=" + str(self.state["trades_today"]) +
            ", pnl=" + str(self.state["daily_pnl_points"]) + "pts" +
            ", consec_losses=" + str(self.state["consecutive_losses"]) + ")"
        )
