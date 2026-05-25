"""
core/trade_state.py
===================
Centralized Trade State Machine for the TradingBot system.

Responsibilities:
    - Own and persist the full lifecycle of a single active trade
    - Prevent duplicate signal generation while a trade is open
    - Survive process restarts (both assistant and tracker)
    - Write outcome data back into the daily Excel trade log when a trade closes
    - Provide clean status transitions between IDLE -> OPEN -> milestones -> IDLE

State machine transitions:
    IDLE          --[open_trade()]---------------> OPEN
    OPEN          --[update_milestone("T1_HIT")]-> TARGET1_HIT
    OPEN / T1     --[update_milestone("T2_HIT")]-> TARGET2_HIT
    Any active    --[close_trade(...)]-----------> IDLE
                     (final outcome written to Excel before reset)

Persistence:
    - JSON file at DATA_DIR/trade_state.json
    - Atomic writes: write to .tmp, then os.replace() (crash-safe)
    - Both processes call reload() each loop iteration for fresh state
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from config.config import STATE_FILE, TRADE_LOG_DIR

# =========================
# LOGGING
# =========================

logger = logging.getLogger(__name__)

# =========================
# DEFAULT (IDLE) STATE TEMPLATE
# =========================

_DEFAULT_STATE: dict = {
    "version":             1,
    "status":              "IDLE",
    "trade_id":            None,
    "signal":              None,
    "direction":           None,
    "lots":                 1,
    "entry_price":         None,
    "entry_time":          None,
    "entry_date":          None,
    "trend":               None,
    "vwap_at_entry":       None,
    "ema9_at_entry":       None,
    # ---- Option premium / capital tracking ----
    "option_premium":      None,
    "premium_source":      None,
    "capital_invested":    None,
    # ---- Exit fields ----
    "exit_price":          None,
    "exit_time":           None,
    "points_result":       None,
    "outcome":             None,
    "milestones_hit":      [],
    "last_tracker_result": None,
    "last_updated":        None,
}


# =========================
# TRADE STATE MANAGER
# =========================

class TradeStateManager:
    """
    Manages the full lifecycle of a single active NIFTY paper trade.
    One instance per process. Call reload() at the top of each loop iteration.
    """

    # =========================
    # STATUS CONSTANTS
    # =========================

    IDLE         = "IDLE"
    OPEN         = "OPEN"
    TARGET1_HIT  = "TARGET1_HIT"
    TARGET2_HIT  = "TARGET2_HIT"
    TARGET3_HIT  = "TARGET3_HIT"
    SL_HIT       = "SL_HIT"
    EOD_CLOSE    = "EOD_CLOSE"

    ACTIVE_STATUSES: frozenset = frozenset({OPEN, TARGET1_HIT, TARGET2_HIT})
    TERMINAL_STATUSES: frozenset = frozenset({TARGET3_HIT, SL_HIT})

    OUTCOME_LABELS: dict = {
        TARGET3_HIT: "TARGET 3 HIT",
        SL_HIT:      "SL HIT",
        EOD_CLOSE:   "EOD CLOSE",
    }

    # =========================
    # INIT
    # =========================

    def __init__(self, state_file: Path = STATE_FILE) -> None:
        self._state_file: Path = Path(state_file)
        self._tmp_file:   Path = self._state_file.with_suffix(".tmp")
        self.state: dict       = self._load()
        logger.info(
            f"[STATE] TradeStateManager initialized | "
            f"status={self.state['status']} | "
            f"trade_id={self.state['trade_id']}"
        )

    # =========================
    # PERSISTENCE: LOAD
    # =========================

    def _load(self) -> dict:
        if not self._state_file.exists():
            logger.info("[STATE] No state file found — starting fresh in IDLE state")
            return dict(_DEFAULT_STATE)
        try:
            with open(self._state_file, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            merged = dict(_DEFAULT_STATE)
            merged.update(loaded)
            logger.info(
                f"[STATE] Loaded from disk: status={merged['status']} | "
                f"trade_id={merged['trade_id']}"
            )
            return merged
        except (json.JSONDecodeError, IOError) as exc:
            logger.critical(
                f"[STATE] State file corrupted or unreadable: {exc}\n"
                f"         Falling back to IDLE."
            )
            return dict(_DEFAULT_STATE)

    # =========================
    # PERSISTENCE: SAVE (ATOMIC)
    # =========================

    def _save(self) -> None:
        self.state["last_updated"] = datetime.now().isoformat()
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._tmp_file, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2, ensure_ascii=False)
            os.replace(str(self._tmp_file), str(self._state_file))
            logger.debug(f"[STATE] Saved to disk: status={self.state['status']}")
        except IOError as exc:
            logger.error(f"[STATE] Failed to save state file: {exc}")

    # =========================
    # RELOAD
    # =========================

    def reload(self) -> None:
        self.state = self._load()

    # =========================
    # STATUS QUERIES
    # =========================

    def has_active_trade(self) -> bool:
        return self.state["status"] in self.ACTIVE_STATUSES

    def is_idle(self) -> bool:
        return self.state["status"] == self.IDLE

    def get_status(self) -> str:
        return self.state["status"]

    def get_active_trade(self) -> Optional[dict]:
        if not self.has_active_trade():
            return None
        return dict(self.state)

    def get_last_tracker_result(self) -> Optional[str]:
        return self.state.get("last_tracker_result")

    def milestone_already_hit(self, milestone: str) -> bool:
        return milestone in self.state.get("milestones_hit", [])

    # =========================
    # TRANSITION: OPEN TRADE
    # =========================

    def open_trade(
        self,
        signal:           str,
        entry_price:      float,
        trend:            str,
        vwap:             float,
        ema9:             float,
        lots:             int   = 1,
        option_premium:   float = None,
        premium_source:   str   = None,
        capital_invested: float = None,
    ) -> str:
        """
        Transition from IDLE → OPEN.

        Args:
            signal:           Trade signal string e.g. "BUY 23800 CE"
            entry_price:      NIFTY index price at entry
            trend:            MTF trend label
            vwap:             VWAP at entry
            ema9:             EMA9 at entry
            lots:             Number of lots (default 1)
            option_premium:   Option LTP in points (fetched from NSE or estimated)
            premium_source:   "nse_api" | "estimated" | None
            capital_invested: Total premium paid in INR (premium × lot_size × lots)

        Returns:
            trade_id string
        """
        if self.has_active_trade():
            raise RuntimeError(
                f"[STATE] Cannot open trade: a trade is already active "
                f"(status={self.state['status']}, id={self.state['trade_id']})"
            )
        now       = datetime.now()
        trade_id  = now.strftime("%Y%m%d_%H%M%S")
        direction = "CE" if "CE" in signal else "PE"
        self.state = {
            **dict(_DEFAULT_STATE),
            "version":          1,
            "status":           self.OPEN,
            "trade_id":         trade_id,
            "signal":           signal,
            "direction":        direction,
            "lots":             lots,
            "entry_price":      entry_price,
            "entry_time":       now.strftime("%H:%M:%S"),
            "entry_date":       now.strftime("%Y-%m-%d"),
            "trend":            trend,
            "vwap_at_entry":    vwap,
            "ema9_at_entry":    ema9,
            "option_premium":   option_premium,
            "premium_source":   premium_source,
            "capital_invested": capital_invested,
            "milestones_hit":   [],
            "last_tracker_result": None,
        }
        self._save()
        logger.info(
            f"[STATE] Trade OPENED | id={trade_id} | signal={signal} | "
            f"entry={entry_price:.2f} | direction={direction} | lots={lots} | "
            f"premium={option_premium} ({premium_source}) | capital=₹{capital_invested}"
        )
        return trade_id

    # =========================
    # TRANSITION: UPDATE MILESTONE
    # =========================

    def update_milestone(self, milestone: str) -> None:
        if not self.has_active_trade():
            logger.warning(f"[STATE] update_milestone({milestone}) called but no active trade.")
            return
        if milestone in self.state.get("milestones_hit", []):
            logger.debug(f"[STATE] Milestone {milestone} already recorded.")
            return
        self.state["status"] = milestone
        self.state["milestones_hit"].append(milestone)
        self.state["last_tracker_result"] = milestone
        self._save()
        logger.info(f"[STATE] Milestone recorded: {milestone}")
        self._update_excel_row({"Trade Status": milestone})

    # =========================
    # TRANSITION: CLOSE TRADE
    # =========================

    def close_trade(
        self,
        exit_price:    float,
        outcome:       str,
        points_result: float,
    ) -> None:
        if not self.has_active_trade():
            logger.warning("[STATE] close_trade() called but no active trade. Ignoring.")
            return
        now      = datetime.now()
        trade_id = self.state["trade_id"]
        logger.info(
            f"[STATE] Closing trade | id={trade_id} | outcome={outcome} | "
            f"points={points_result:+.2f} | exit={exit_price:.2f}"
        )
        self._update_excel_row({
            "Trade Status":  "CLOSED",
            "Outcome":       outcome,
            "Points Result": round(points_result, 2),
            "Exit Price":    exit_price,
            "Exit Time":     now.strftime("%H:%M:%S"),
        })
        self.state = dict(_DEFAULT_STATE)
        self._save()
        logger.info(f"[STATE] Trade CLOSED and state reset to IDLE | id={trade_id}")

    # =========================
    # TRACKER DEDUP PERSISTENCE
    # =========================

    def set_last_tracker_result(self, result: str) -> None:
        self.state["last_tracker_result"] = result
        self._save()

    # =========================
    # EMERGENCY RESET
    # =========================

    def reset_to_idle(self) -> None:
        logger.warning(
            "[STATE] Emergency reset to IDLE invoked. "
            f"Previous state: {self.state['status']} / {self.state['trade_id']}"
        )
        self.state = dict(_DEFAULT_STATE)
        self._save()


    # =========================
    # EXCEL ROW CREATE (PRIVATE)
    # Called by trading_engine._open_trade() to stamp the initial row.
    # Close/milestone updates use _update_excel_row() below.
    # =========================

    def create_excel_row(self, sig: dict, lots: int = 1) -> None:
        """
        Write the opening row to trade_log_YYYY-MM-DD.xlsx.
        Called once at trade open with the full signal dict from SignalEngine.
        The row is later updated by _update_excel_row() on close/milestone.
        """
        from config.config import (
            STOP_LOSS_POINTS, TARGET_1_POINTS, TARGET_2_POINTS, TARGET_3_POINTS,
        )
        entry_date = self.state.get("entry_date")
        trade_id   = self.state.get("trade_id")
        if entry_date is None:
            logger.error("[STATE] create_excel_row: entry_date is None — skipping")
            return

        now = datetime.now()
        tf_data = sig.get("timeframe_data", {})
        oi      = sig.get("oi_result",   {})
        sent    = sig.get("sent_result", {})

        # Estimated max loss in INR (SL pts × 37.5 per point per lot)
        max_loss_inr = round(STOP_LOSS_POINTS * lots * 75 * 0.5, 2)

        row = {
            "Date":             entry_date,
            "Time":             now.strftime("%H:%M:%S"),
            "Trend":            sig.get("trend", ""),
            "Current Price":    sig.get("price", 0),
            "VWAP":             sig.get("vwap", 0),
            "EMA9":             sig.get("ema9", 0),
            "Trade Signal":     sig.get("trade_signal", ""),
            "Stop Loss":        f"{STOP_LOSS_POINTS} Points",
            "Target 1":         f"{TARGET_1_POINTS} Points",
            "Target 2":         f"{TARGET_2_POINTS} Points",
            "Target 3":         f"{TARGET_3_POINTS} Points",
            "Lots":             lots,
            "Max Loss INR":     max_loss_inr,
            "MTF Alignment":    sig.get("alignment_summary", ""),
            "Base Score":       sig.get("base_score", None),
            "OI Adjustment":    oi.get("score_adjustment", None),
            "Sentiment Adj":    sent.get("score_adjustment", None),
            "Confidence Score": sig.get("adjusted_score", None),
            "Confidence Level": sig.get("confidence_level", ""),
            "TF 5m":            tf_data.get("5m", {}).get("direction", ""),
            "TF 15m":           tf_data.get("15m", {}).get("direction", ""),
            "TF 1h":            tf_data.get("1h", {}).get("direction", ""),
            "PCR":              oi.get("pcr", None),
            "Max Pain":         oi.get("max_pain", None),
            "ATM OI Bias":      oi.get("atm_bias", None),
            "India VIX":        sent.get("vix", None),
            "US Futures Pct":   sent.get("us_futures_pct", None),
            "News Sentiment":   sent.get("mood", None),
            "Trade ID":         trade_id,
            "Trade Status":     "OPEN",
            "Outcome":          None,
            "Points Result":    None,
            "Exit Price":       None,
            "Exit Time":        None,
        }

        log_file = TRADE_LOG_DIR / f"trade_log_{entry_date}.xlsx"
        tmp_file = log_file.with_suffix(".tmp.xlsx")
        try:
            import pandas as pd
            if log_file.exists():
                df_existing = pd.read_excel(log_file, engine="openpyxl")
                df_new      = pd.concat(
                    [df_existing, pd.DataFrame([row])], ignore_index=True
                )
            else:
                df_new = pd.DataFrame([row])
            df_new.to_excel(tmp_file, index=False, engine="openpyxl")
            import os
            os.replace(str(tmp_file), str(log_file))
            logger.info(
                f"[STATE] Excel row created | id={trade_id} | file={log_file.name}"
            )
        except Exception as exc:
            logger.error(f"[STATE] Failed to create Excel row: {exc}")

    # =========================
    # EXCEL ROW UPDATE (PRIVATE)
    # =========================

    def _update_excel_row(self, updates: dict) -> None:
        entry_date = self.state.get("entry_date")
        trade_id   = self.state.get("trade_id")
        if entry_date is None:
            logger.error("[STATE] _update_excel_row: entry_date is None")
            return
        log_file = TRADE_LOG_DIR / f"trade_log_{entry_date}.xlsx"
        if not log_file.exists():
            logger.error(f"[STATE] Cannot update Excel: log file not found: {log_file.name}")
            return
        try:
            df = pd.read_excel(log_file, engine="openpyxl")
            if df.empty:
                logger.error("[STATE] Cannot update Excel: log file is empty")
                return
            target_idx = None
            if "Trade ID" in df.columns and trade_id is not None:
                matched = df[df["Trade ID"] == trade_id]
                if not matched.empty:
                    target_idx = matched.index[-1]
                else:
                    logger.warning(f"[STATE] Trade ID '{trade_id}' not found in Excel.")
            if target_idx is None:
                signal_mask = (
                    df["Trade Signal"].notna() &
                    (df["Trade Signal"] != "NO TRADE")
                )
                target_idx = df[signal_mask].index[-1] if signal_mask.any() else df.index[-1]
                logger.warning(f"[STATE] Using fallback row index {target_idx}")
            new_columns = ["Trade ID", "Trade Status", "Outcome", "Points Result", "Exit Price", "Exit Time"]
            for col in new_columns:
                if col not in df.columns:
                    df[col] = None
            for col, val in updates.items():
                df.at[target_idx, col] = val
            df.to_excel(log_file, index=False, engine="openpyxl")
            logger.info(
                f"[STATE] Excel updated | row={target_idx} | "
                f"file={log_file.name} | updates={list(updates.keys())}"
            )
        except Exception as exc:
            logger.error(f"[STATE] Failed to update Excel row: {exc}")

    # =========================
    # REPR
    # =========================

    def __repr__(self) -> str:
        return (
            f"TradeStateManager("
            f"status={self.state['status']!r}, "
            f"trade_id={self.state['trade_id']!r}, "
            f"signal={self.state['signal']!r})"
        )
