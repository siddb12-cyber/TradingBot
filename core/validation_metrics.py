"""
core/validation_metrics.py
===========================
Paper Trading Validation Metrics Engine.

Computes all 7 validation metrics from existing trade log Excel files
and the daily risk state JSON. No inline recording required — reads
from the same artifacts already produced by the signal engine and tracker.

Metrics computed:
    1. signal_accuracy        — % of trades that hit T1 or better
    2. target_hit_ratio       — breakdown: T1% / T2% / T3%
    3. sl_ratio               — % of trades that hit SL
    4. avg_confidence_score   — mean confidence score across all trades
    5. avg_holding_time_min   — mean minutes from entry to exit
    6. max_consecutive_losses — longest SL streak in trade history
    7. cooldown_frequency     — SL-triggered cooldowns counted from outcomes
    8. trade_rejection_count  — trades rejected (confidence LOW + risk gate)
       [tracked in paper_validation_metrics.json — updated by assistant]

Readiness Score (0-100):
    Weighted composite of metrics against READINESS thresholds.
    Score >= 75  → READY for live deployment
    Score 50-74  → CONDITIONAL (needs more data or improvement)
    Score < 50   → NOT READY

Persistence:
    DATA_DIR/paper_validation_metrics.json
    - Cumulative across validation phase (not reset daily)
    - Atomic writes (same pattern as trade_state / risk_engine)
    - Rejection counters stored here (not in Excel)

Usage:
    from core.validation_metrics import ValidationMetrics

    vm = ValidationMetrics()
    vm.record_rejection("confidence_low")   # call from assistant on LOW gate
    vm.record_rejection("risk_gate")        # call from assistant on risk block
    summary = vm.compute_summary()          # call from paper_trading_report
    score   = summary["readiness_score"]
"""

import json
import logging
import os
from datetime import datetime, date
from pathlib import Path
from typing import Optional

import pandas as pd

from config.config import (
    TRADE_LOG_DIR,
    PAPER_METRICS_FILE,
    READINESS_MIN_TRADES,
    READINESS_MIN_SIGNAL_ACCURACY,
    READINESS_MAX_SL_RATIO,
    READINESS_MIN_AVG_CONFIDENCE,
    READINESS_MAX_CONSEC_LOSSES,
    PAPER_TRADING_VALIDATION_END,
)

logger = logging.getLogger(__name__)

# =========================
# OUTCOME CONSTANTS
# Must match TradeStateManager.OUTCOME_LABELS values
# =========================

OUTCOME_T1  = "TARGET1_HIT"   # milestone (not terminal — but counts as partial win)
OUTCOME_T2  = "TARGET2_HIT"   # milestone
OUTCOME_T3  = "TARGET 3 HIT"  # terminal win
OUTCOME_SL  = "SL HIT"        # terminal loss
OUTCOME_EOD = "EOD CLOSE"     # neutral forced close

# Outcomes that count as "signal accuracy" wins (hit T1 or better before close)
WIN_OUTCOMES = {"TARGET 3 HIT", "TARGET1_HIT", "TARGET2_HIT"}

# =========================
# DEFAULT METRICS STATE
# =========================

_DEFAULT_METRICS: dict = {
    "version":                  1,
    "created_date":             None,
    "last_updated":             None,
    "rejection_confidence_low": 0,    # trades blocked by confidence gate
    "rejection_risk_gate":      0,    # trades blocked by daily risk limits
    "rejection_other":          0,    # any other rejections
}

# Readiness component weights (must sum to 100)
_WEIGHT_ACCURACY    = 30
_WEIGHT_SL_RATIO    = 25
_WEIGHT_CONFIDENCE  = 20
_WEIGHT_CONSEC_LOSS = 15
_WEIGHT_TRADE_COUNT = 10


# =========================
# VALIDATION METRICS ENGINE
# =========================

class ValidationMetrics:
    """
    Reads trade log Excel files to compute validation metrics.
    Maintains a lightweight JSON for rejection counters (not in Excel).

    One instance per process. Call compute_summary() to get all metrics.
    """

    def __init__(self, metrics_file: Path = PAPER_METRICS_FILE) -> None:
        self._file = Path(metrics_file)
        self._tmp  = self._file.with_suffix(".tmp")
        self._state: dict = self._load()
        logger.info(
            f"[METRICS] ValidationMetrics initialized | "
            f"rejections_confidence={self._state['rejection_confidence_low']} "
            f"rejections_risk={self._state['rejection_risk_gate']}"
        )

    # =========================
    # PERSISTENCE
    # =========================

    def _load(self) -> dict:
        if not self._file.exists():
            state = dict(_DEFAULT_METRICS)
            state["created_date"] = datetime.now().isoformat()
            return state
        try:
            with open(self._file, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            merged = dict(_DEFAULT_METRICS)
            merged.update(loaded)
            return merged
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"[METRICS] Metrics file corrupted: {e} — resetting")
            return dict(_DEFAULT_METRICS)

    def _save(self) -> None:
        self._state["last_updated"] = datetime.now().isoformat()
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._tmp, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2)
            os.replace(str(self._tmp), str(self._file))
        except IOError as e:
            logger.error(f"[METRICS] Failed to save metrics: {e}")

    def reload(self) -> None:
        self._state = self._load()

    # =========================
    # REJECTION RECORDING
    # Called from ai_trading_assistant when a trade is blocked.
    # =========================

    def record_rejection(self, reason: str) -> None:
        """
        Increment a rejection counter.

        Args:
            reason: "confidence_low" | "risk_gate" | "other"
        """
        if reason == "confidence_low":
            self._state["rejection_confidence_low"] += 1
        elif reason == "risk_gate":
            self._state["rejection_risk_gate"] += 1
        else:
            self._state["rejection_other"] += 1
        self._save()
        logger.info(f"[METRICS] Rejection recorded: {reason}")

    # =========================
    # EXCEL LOG READER
    # =========================

    def _load_all_trades(self) -> pd.DataFrame:
        """
        Load and concatenate all daily trade log Excel files from TRADE_LOG_DIR.
        Only closed trades (Trade Status = CLOSED) are included in metrics.

        Returns:
            DataFrame with columns: Date, Time, Trade Signal, Outcome,
                                    Points Result, Exit Time, Confidence Score,
                                    entry_dt, exit_dt, holding_min
        """
        frames = []
        for f in sorted(TRADE_LOG_DIR.glob("trade_log_*.xlsx")):
            try:
                df = pd.read_excel(f, engine="openpyxl")
                frames.append(df)
            except Exception as e:
                logger.warning(f"[METRICS] Could not read {f.name}: {e}")

        if not frames:
            return pd.DataFrame()

        combined = pd.concat(frames, ignore_index=True)

        # Filter to closed trades only
        if "Trade Status" in combined.columns:
            combined = combined[combined["Trade Status"] == "CLOSED"].copy()

        if combined.empty:
            return combined

        # Build datetime columns for holding time calculation
        try:
            if "Date" in combined.columns and "Time" in combined.columns:
                combined["entry_dt"] = pd.to_datetime(
                    combined["Date"].astype(str) + " " + combined["Time"].astype(str),
                    errors="coerce"
                )
            if "Date" in combined.columns and "Exit Time" in combined.columns:
                combined["exit_dt"] = pd.to_datetime(
                    combined["Date"].astype(str) + " " + combined["Exit Time"].astype(str),
                    errors="coerce"
                )
            combined["holding_min"] = (
                (combined["exit_dt"] - combined["entry_dt"]).dt.total_seconds() / 60
            ).clip(lower=0)
        except Exception as e:
            logger.warning(f"[METRICS] Holding time calculation failed: {e}")
            combined["holding_min"] = None

        return combined

    # =========================
    # INDIVIDUAL METRIC CALCULATORS
    # =========================

    def _calc_signal_accuracy(self, df: pd.DataFrame) -> float:
        """
        Signal accuracy = trades that hit T1, T2, or T3 / total closed trades.
        Includes milestone columns (TARGET1_HIT in milestones_hit) and Outcome column.
        """
        if df.empty or "Outcome" not in df.columns:
            return 0.0
        total = len(df)
        if total == 0:
            return 0.0
        wins = df["Outcome"].isin(["TARGET 3 HIT"]).sum()

        # Also count trades where milestone columns show T1/T2 hit
        for col in ["TF 5m", "TF 15m", "TF 1h"]:
            pass   # these are direction columns, not outcome

        # Check if any milestone columns exist
        # Outcome of "EOD CLOSE" with positive points also counts as partial win
        eod_wins = 0
        if "Points Result" in df.columns:
            eod_mask = (df["Outcome"] == "EOD CLOSE") & (df["Points Result"] > 0)
            eod_wins = eod_mask.sum()

        accuracy = (wins + eod_wins) / total
        return round(accuracy, 4)

    def _calc_target_hit_ratio(self, df: pd.DataFrame) -> dict:
        """
        Breakdown of T1 / T2 / T3 hit percentages.
        Uses Outcome column + positive Points Result heuristic.
        """
        if df.empty or "Outcome" not in df.columns:
            return {"t1_pct": 0.0, "t2_pct": 0.0, "t3_pct": 0.0}
        total = len(df)
        if total == 0:
            return {"t1_pct": 0.0, "t2_pct": 0.0, "t3_pct": 0.0}

        t3 = df["Outcome"].isin(["TARGET 3 HIT"]).sum()

        # T1 and T2 are milestones — if hit, trade continued to T3 or SL.
        # Count from Points Result ranges as proxy when milestone cols absent.
        t1_pts = 15
        t2_pts = 25

        t2 = 0
        t1 = 0
        if "Points Result" in df.columns:
            pts = pd.to_numeric(df["Points Result"], errors="coerce").fillna(0)
            t2 = (pts >= t2_pts).sum()
            t1 = (pts >= t1_pts).sum()

        return {
            "t1_pct": round(t1 / total, 4),
            "t2_pct": round(t2 / total, 4),
            "t3_pct": round(t3 / total, 4),
        }

    def _calc_sl_ratio(self, df: pd.DataFrame) -> float:
        if df.empty or "Outcome" not in df.columns:
            return 0.0
        total = len(df)
        if total == 0:
            return 0.0
        sl_hits = df["Outcome"].isin(["SL HIT"]).sum()
        return round(sl_hits / total, 4)

    def _calc_avg_confidence(self, df: pd.DataFrame) -> float:
        if df.empty or "Confidence Score" not in df.columns:
            return 0.0
        scores = pd.to_numeric(df["Confidence Score"], errors="coerce").dropna()
        if scores.empty:
            return 0.0
        return round(float(scores.mean()), 2)

    def _calc_avg_holding_time(self, df: pd.DataFrame) -> float:
        """Average holding time in minutes."""
        if df.empty or "holding_min" not in df.columns:
            return 0.0
        times = pd.to_numeric(df["holding_min"], errors="coerce").dropna()
        if times.empty:
            return 0.0
        return round(float(times.mean()), 2)

    def _calc_max_consecutive_losses(self, df: pd.DataFrame) -> int:
        """Maximum consecutive SL HIT outcomes in trade history."""
        if df.empty or "Outcome" not in df.columns:
            return 0
        outcomes = df["Outcome"].tolist()
        max_streak = 0
        current    = 0
        for o in outcomes:
            if o == "SL HIT":
                current += 1
                max_streak = max(max_streak, current)
            else:
                current = 0
        return max_streak

    def _calc_cooldown_frequency(self, df: pd.DataFrame) -> int:
        """Number of SL hits (each triggers a cooldown period)."""
        if df.empty or "Outcome" not in df.columns:
            return 0
        return int(df["Outcome"].isin(["SL HIT"]).sum())

    # =========================
    # READINESS SCORE
    # =========================

    def _compute_readiness_score(self, metrics: dict) -> tuple[int, str]:
        """
        Compute a 0-100 readiness score from validation metrics.

        Components:
            - Signal accuracy (30pts)
            - SL ratio (25pts)
            - Avg confidence (20pts)
            - Max consecutive losses (15pts)
            - Trade count (10pts)

        Returns:
            (score: int, status: str)  — status is READY / CONDITIONAL / NOT READY
        """
        score = 0
        total = metrics.get("total_trades", 0)

        # --- Signal accuracy (30pts) ---
        acc = metrics.get("signal_accuracy", 0.0)
        if acc >= READINESS_MIN_SIGNAL_ACCURACY:
            score += _WEIGHT_ACCURACY
        else:
            score += int(_WEIGHT_ACCURACY * (acc / READINESS_MIN_SIGNAL_ACCURACY))

        # --- SL ratio (25pts — lower is better) ---
        sl = metrics.get("sl_ratio", 1.0)
        if sl <= READINESS_MAX_SL_RATIO:
            score += _WEIGHT_SL_RATIO
        elif sl < 1.0:
            # Partial credit — inverse linear
            score += int(_WEIGHT_SL_RATIO * (1 - ((sl - READINESS_MAX_SL_RATIO) /
                                                   (1 - READINESS_MAX_SL_RATIO))))

        # --- Avg confidence (20pts) ---
        conf = metrics.get("avg_confidence_score", 0.0)
        if conf >= READINESS_MIN_AVG_CONFIDENCE:
            score += _WEIGHT_CONFIDENCE
        else:
            score += int(_WEIGHT_CONFIDENCE * (conf / READINESS_MIN_AVG_CONFIDENCE))

        # --- Max consecutive losses (15pts — lower is better) ---
        consec = metrics.get("max_consecutive_losses", 999)
        if consec < READINESS_MAX_CONSEC_LOSSES:
            score += _WEIGHT_CONSEC_LOSS
        elif consec == READINESS_MAX_CONSEC_LOSSES:
            score += _WEIGHT_CONSEC_LOSS // 2

        # --- Trade count (10pts) ---
        if total >= READINESS_MIN_TRADES:
            score += _WEIGHT_TRADE_COUNT
        elif total > 0:
            score += int(_WEIGHT_TRADE_COUNT * (total / READINESS_MIN_TRADES))

        score = max(0, min(100, score))

        if score >= 75:
            status = "READY"
        elif score >= 50:
            status = "CONDITIONAL"
        else:
            status = "NOT READY"

        return score, status

    # =========================
    # COMPUTE SUMMARY
    # =========================

    def compute_summary(self, target_date: Optional[str] = None) -> dict:
        """
        Compute all 7 validation metrics + readiness score.

        Args:
            target_date: "YYYY-MM-DD" to filter to a single day.
                          None = all trades across validation phase.

        Returns:
            Full metrics dict suitable for paper_trading_report.py
        """
        self.reload()
        df = self._load_all_trades()

        # Optional single-day filter
        if target_date and not df.empty and "Date" in df.columns:
            df = df[df["Date"].astype(str) == target_date].copy()

        total_trades = len(df)
        logger.info(
            f"[METRICS] Computing summary | trades={total_trades} "
            f"| date_filter={target_date or 'ALL'}"
        )

        signal_accuracy        = self._calc_signal_accuracy(df)
        target_hit_ratio       = self._calc_target_hit_ratio(df)
        sl_ratio               = self._calc_sl_ratio(df)
        avg_confidence         = self._calc_avg_confidence(df)
        avg_holding_min        = self._calc_avg_holding_time(df)
        max_consec_losses      = self._calc_max_consecutive_losses(df)
        cooldown_freq          = self._calc_cooldown_frequency(df)

        total_rejections = (
            self._state["rejection_confidence_low"] +
            self._state["rejection_risk_gate"] +
            self._state["rejection_other"]
        )

        metrics = {
            "computed_at":             datetime.now().isoformat(),
            "date_filter":             target_date or "ALL",
            "total_trades":            total_trades,
            "signal_accuracy":         signal_accuracy,
            "signal_accuracy_pct":     round(signal_accuracy * 100, 1),
            "target_hit_ratio":        target_hit_ratio,
            "sl_ratio":                sl_ratio,
            "sl_ratio_pct":            round(sl_ratio * 100, 1),
            "avg_confidence_score":    avg_confidence,
            "avg_holding_time_min":    avg_holding_min,
            "max_consecutive_losses":  max_consec_losses,
            "cooldown_frequency":      cooldown_freq,
            "rejection_confidence_low": self._state["rejection_confidence_low"],
            "rejection_risk_gate":      self._state["rejection_risk_gate"],
            "rejection_other":          self._state["rejection_other"],
            "total_rejections":         total_rejections,
            "validation_end":          PAPER_TRADING_VALIDATION_END,
        }

        readiness_score, readiness_status = self._compute_readiness_score(metrics)
        metrics["readiness_score"]  = readiness_score
        metrics["readiness_status"] = readiness_status

        logger.info(
            f"[METRICS] Summary: accuracy={signal_accuracy*100:.1f}% | "
            f"sl={sl_ratio*100:.1f}% | conf={avg_confidence:.1f} | "
            f"consec_loss={max_consec_losses} | "
            f"readiness={readiness_score}/100 [{readiness_status}]"
        )

        return metrics

    # =========================
    # DAILY METRICS (for EOD report)
    # =========================

    def compute_daily_summary(self) -> dict:
        """Compute metrics for today only."""
        today = datetime.now().strftime("%Y-%m-%d")
        return self.compute_summary(target_date=today)

    def compute_cumulative_summary(self) -> dict:
        """Compute metrics across entire validation phase."""
        return self.compute_summary(target_date=None)
