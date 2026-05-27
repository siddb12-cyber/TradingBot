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

Smart Cooldown Override (Priority 6 upgrade):
    After an SL hit the system enters a 30-min cooldown, but does NOT block blindly.
    Overrides are evaluated on every new signal:
      - HIGH confidence (>=CONFIDENCE_HIGH_THRESHOLD): always bypasses cooldown
      - Opposite-direction signal with MEDIUM+ confidence: bypasses cooldown (market reversed)
      - Same direction + MEDIUM confidence: cooldown respected (revenge-trade protection)
    Daily loss limit and consecutive-loss lockout are HARD stops — never overridden.

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

    check = risk.check_trade_allowed(signal_direction="BULLISH", adjusted_score=72)
    if not check["allowed"]:
        print(check["reason"])
        return

    sizing = risk.calculate_position_size()
    # sizing = {"lots": 2, "max_loss_inr": 750.0, "risk_pct": 15.0}

    risk.record_trade_opened()
    risk.record_trade_closed("SL HIT", -10.0, direction="BULLISH")
"""

import json
import logging
import math
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from config.settings import (
    DAILY_RISK_STATE_FILE,
    ACCOUNT_CAPITAL, MAX_RISK_PCT, MAX_DAILY_LOSS_PCT,
    MAX_TRADES_PER_DAY, COOLDOWN_AFTER_SL_MINUTES, MAX_CONSECUTIVE_LOSSES,
    NIFTY_LOT_SIZE, OPTION_DELTA, STOP_LOSS_POINTS,
    COOLDOWN_HIGH_CONF_OVERRIDE, COOLDOWN_REVERSAL_OVERRIDE,
    CONFIDENCE_HIGH_THRESHOLD,
    TRADE_EXTENSION_BATCH, TRADE_EXTENSION_MAX_TOTAL, TRADE_EXTENSION_MIN_SCORE,
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
        "date":                  today,
        "trades_today":          0,
        "trades_won":            0,       # Trades that closed at T1 / T2 / T3 or positive EOD
        "trades_lost":           0,       # Trades that closed at SL or negative EOD
        "daily_pnl_points":      0.0,
        "gross_profit_points":   0.0,     # Sum of winning trade points
        "gross_loss_points":     0.0,     # Sum of losing trade points (stored as negative)
        "consecutive_losses":    0,
        "consecutive_wins":      0,       # Current winning streak
        "max_consecutive_wins":  0,       # Best win streak today
        "max_consecutive_losses":0,       # Worst loss streak today
        "cooldown_until":        None,
        "last_sl_direction":     None,
        "trade_limit_extension": 0,
        "last_updated":          None,
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
                new_state = _empty_state(today)
                # Persist immediately so subsequent reload() calls find today's date
                # and do NOT re-trigger this reset on every call (infinite reset bug).
                try:
                    self._state_file.parent.mkdir(parents=True, exist_ok=True)
                    with open(self._tmp_file, "w", encoding="utf-8") as _f:
                        json.dump(new_state, _f, indent=2)
                    os.replace(str(self._tmp_file), str(self._state_file))
                    logger.debug("[RISK] New-day state persisted to disk")
                except IOError as _exc:
                    logger.warning("[RISK] Could not persist new-day state: " + str(_exc))
                return new_state

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
                lots         (int)   -- suggested number of lots
                max_loss_inr (float) -- estimated max loss if SL is hit
                risk_pct     (float) -- actual risk % of capital
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

    def check_trade_allowed(
        self,
        signal_direction: Optional[str] = None,
        adjusted_score: int = 0,
    ) -> dict:
        """
        Check all daily risk rules before allowing a new trade.

        Smart cooldown override logic (replaces blanket 30-min block):
          - HIGH confidence (>=CONFIDENCE_HIGH_THRESHOLD): bypass cooldown entirely
          - Opposite direction to last SL trade + MEDIUM+ confidence: bypass (market reversed)
          - Same direction as last SL trade + MEDIUM confidence: respect cooldown

        Args:
            signal_direction: "BULLISH" or "BEARISH" -- current signal direction
            adjusted_score:   Final confidence score after OI + sentiment adjustments

        Returns:
            {"allowed": True,  "reason": ""}         -- trade permitted
            {"allowed": False, "reason": "<why>"}    -- trade blocked
        """
        now = datetime.now()

        # 1. Max trades per day (base limit + any Telegram-approved extension)
        extension    = self.state.get("trade_limit_extension", 0)
        effective_max = MAX_TRADES_PER_DAY + extension
        if self.state["trades_today"] >= effective_max:
            at_base_limit = (self.state["trades_today"] >= MAX_TRADES_PER_DAY)
            total_extension_used = extension
            can_extend = (
                at_base_limit
                and adjusted_score >= TRADE_EXTENSION_MIN_SCORE
                and total_extension_used < TRADE_EXTENSION_MAX_TOTAL
            )
            reason = (
                "Max trades/day reached: " + str(self.state["trades_today"]) +
                "/" + str(effective_max) +
                (" (extended)" if extension > 0 else "")
            )
            logger.warning("[RISK] BLOCKED — " + reason)
            return {
                "allowed":       False,
                "reason":        reason,
                "at_daily_limit": True,
                "can_extend":    can_extend,   # True → engine should ask user for extension
            }

        # 2. Max daily loss (hard stop — cannot be overridden)
        max_loss_inr   = ACCOUNT_CAPITAL * MAX_DAILY_LOSS_PCT / 100
        daily_loss_inr = abs(self.state["daily_pnl_points"]) * OPTION_DELTA * NIFTY_LOT_SIZE
        if self.state["daily_pnl_points"] < 0 and daily_loss_inr >= max_loss_inr:
            reason = (
                "Daily loss limit hit: Rs." + str(round(daily_loss_inr, 0)) +
                " >= Rs." + str(round(max_loss_inr, 0)) +
                " (" + str(MAX_DAILY_LOSS_PCT) + "% of capital)"
            )
            logger.warning("[RISK] BLOCKED — " + reason)
            return {"allowed": False, "reason": reason}

        # 3. SL cooldown — with smart direction-aware override
        if self.state["cooldown_until"] is not None:
            try:
                cooldown_dt = datetime.fromisoformat(self.state["cooldown_until"])
                if now < cooldown_dt:
                    remaining       = max(1, int((cooldown_dt - now).total_seconds() / 60))
                    last_sl_dir     = self.state.get("last_sl_direction")
                    override        = False
                    override_reason = ""

                    # Override A: HIGH confidence signal overrides cooldown entirely
                    if (COOLDOWN_HIGH_CONF_OVERRIDE
                            and adjusted_score >= CONFIDENCE_HIGH_THRESHOLD):
                        override        = True
                        override_reason = (
                            "HIGH confidence (" + str(adjusted_score) + "/100) "
                            "overrides " + str(remaining) + "m cooldown"
                        )

                    # Override B: opposite-direction signal overrides cooldown (market reversed)
                    elif (COOLDOWN_REVERSAL_OVERRIDE
                            and signal_direction
                            and last_sl_dir
                            and signal_direction != last_sl_dir):
                        override        = True
                        override_reason = (
                            "Direction reversal " + str(last_sl_dir) +
                            " -> " + str(signal_direction) +
                            " overrides " + str(remaining) + "m cooldown"
                        )

                    if override:
                        logger.info("[RISK] Cooldown OVERRIDDEN — " + override_reason)
                        self.state["cooldown_until"] = None
                        self._save()
                        # Fall through to remaining checks
                    else:
                        # Same direction + not high confidence — respect cooldown
                        reason = (
                            "SL cooldown active: " + str(remaining) + "m remaining "
                            "(same direction as last SL). "
                            "HIGH confidence or direction reversal will bypass this."
                        )
                        logger.warning("[RISK] BLOCKED — " + reason)
                        return {"allowed": False, "reason": reason}
                else:
                    # Cooldown expired naturally — clear it
                    self.state["cooldown_until"] = None
                    self._save()
            except ValueError:
                self.state["cooldown_until"] = None
                self._save()

        # 4. Consecutive loss lockout (hard stop — cannot be overridden)
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

    def record_trade_closed(
        self,
        outcome: str,
        points_result: float,
        direction: Optional[str] = None,
    ) -> None:
        """
        Update daily P&L, consecutive loss counter, and cooldown after a trade closes.

        Args:
            outcome:       Human-readable outcome string (e.g. "SL HIT", "TARGET 3 HIT")
            points_result: NIFTY index points gained (+) or lost (-)
            direction:     "BULLISH" or "BEARISH" -- direction of the closed trade
                           (stored so next scan can apply smart override if market reverses)
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

        is_win = not is_loss and points_result > 0

        if is_loss:
            # Track loss stats
            self.state["trades_lost"]        = self.state.get("trades_lost", 0) + 1
            self.state["gross_loss_points"]  = round(
                self.state.get("gross_loss_points", 0.0) + points_result, 2
            )
            self.state["consecutive_losses"] = self.state.get("consecutive_losses", 0) + 1
            self.state["consecutive_wins"]   = 0   # Reset win streak
            self.state["max_consecutive_losses"] = max(
                self.state.get("max_consecutive_losses", 0),
                self.state["consecutive_losses"]
            )
            # Set cooldown (only for SL HIT, not EOD) and save the SL direction
            if outcome == "SL HIT":
                cooldown_dt = datetime.now() + timedelta(minutes=COOLDOWN_AFTER_SL_MINUTES)
                self.state["cooldown_until"]    = cooldown_dt.isoformat()
                self.state["last_sl_direction"] = direction
                logger.info(
                    "[RISK] SL hit — cooldown set until " +
                    cooldown_dt.strftime("%H:%M") +
                    " | direction=" + str(direction) +
                    " | HIGH conf or reversal will bypass"
                )
        elif is_win:
            # Track win stats
            self.state["trades_won"]         = self.state.get("trades_won", 0) + 1
            self.state["gross_profit_points"]= round(
                self.state.get("gross_profit_points", 0.0) + points_result, 2
            )
            self.state["consecutive_wins"]   = self.state.get("consecutive_wins", 0) + 1
            self.state["consecutive_losses"] = 0
            self.state["last_sl_direction"]  = None
            self.state["max_consecutive_wins"] = max(
                self.state.get("max_consecutive_wins", 0),
                self.state["consecutive_wins"]
            )
        else:
            # Breakeven / EOD neutral — reset both streaks
            self.state["consecutive_losses"] = 0
            self.state["consecutive_wins"]   = 0
            self.state["last_sl_direction"]  = None

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
    # TRADE LIMIT EXTENSION
    # =========================

    def grant_trade_extension(self, additional: int = None) -> int:
        """
        Grant extra trades for today via Telegram approval.
        Called by trading_engine.py after user approves the extension.

        Args:
            additional: How many extra trades to add (defaults to TRADE_EXTENSION_BATCH)

        Returns:
            New effective daily limit (base + total extension)
        """
        if additional is None:
            additional = TRADE_EXTENSION_BATCH

        current_ext  = self.state.get("trade_limit_extension", 0)
        max_ext      = TRADE_EXTENSION_MAX_TOTAL
        actual_add   = min(additional, max_ext - current_ext)   # Don't exceed ceiling

        self.state["trade_limit_extension"] = current_ext + actual_add
        self._save()

        new_limit = MAX_TRADES_PER_DAY + self.state["trade_limit_extension"]
        logger.info(
            "[RISK] Trade limit extended by %d → new limit: %d/day (total extension: %d)",
            actual_add, new_limit, self.state["trade_limit_extension"],
        )
        return new_limit

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
        extension     = self.state.get("trade_limit_extension", 0)
        effective_max = MAX_TRADES_PER_DAY + extension
        trades_won    = self.state.get("trades_won", 0)
        trades_lost   = self.state.get("trades_lost", 0)
        trades_closed = trades_won + trades_lost
        win_rate      = round(trades_won / trades_closed * 100, 1) if trades_closed > 0 else 0.0
        gross_profit  = self.state.get("gross_profit_points", 0.0)
        gross_loss    = abs(self.state.get("gross_loss_points", 0.0))
        profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else None
        avg_win_pts   = round(gross_profit / trades_won, 2) if trades_won > 0 else 0.0
        avg_loss_pts  = round(gross_loss / trades_lost, 2) if trades_lost > 0 else 0.0
        # Expectancy: (win_rate × avg_win) - (loss_rate × avg_loss)
        loss_rate     = 1 - (win_rate / 100)
        expectancy    = round((win_rate / 100) * avg_win_pts - loss_rate * avg_loss_pts, 2)

        return {
            "date":                   self.state["date"],
            "trades_today":           self.state["trades_today"],
            "trades_won":             trades_won,
            "trades_lost":            trades_lost,
            "max_trades":             effective_max,
            "max_trades_base":        MAX_TRADES_PER_DAY,
            "trade_limit_extension":  extension,
            "daily_pnl_points":       self.state["daily_pnl_points"],
            "daily_pnl_inr":          round(daily_pnl_inr, 2),
            "gross_profit_points":    gross_profit,
            "gross_loss_points":      self.state.get("gross_loss_points", 0.0),
            "profit_factor":          profit_factor,
            "win_rate":               win_rate,
            "avg_win_pts":            avg_win_pts,
            "avg_loss_pts":           avg_loss_pts,
            "expectancy_pts":         expectancy,
            "consecutive_losses":     self.state["consecutive_losses"],
            "consecutive_wins":       self.state.get("consecutive_wins", 0),
            "max_consecutive_wins":   self.state.get("max_consecutive_wins", 0),
            "max_consecutive_losses": self.state.get("max_consecutive_losses", 0),
            "cooldown_until":         self.state["cooldown_until"],
            "suggested_lots":         sizing["lots"],
            "max_loss_inr":           sizing["max_loss_inr"],
            "risk_pct":               sizing["risk_pct"],
        }

    def __repr__(self) -> str:
        return (
            "RiskEngine(date=" + self.state["date"] +
            ", trades=" + str(self.state["trades_today"]) +
            ", pnl=" + str(self.state["daily_pnl_points"]) + "pts" +
            ", consec_losses=" + str(self.state["consecutive_losses"]) + ")"
        )
