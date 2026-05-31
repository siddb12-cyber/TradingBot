"""
analytics/logger.py
===================
Trade and decision logger for TradingBot.

Replaces analytics/decision_logger.py.

Output Folders
--------------
  decisions/YYYY-MM-DD.json   — every signal scan (trade or no-trade)
  trades/YYYY-MM-DD.json      — completed trades only

Format
------
Each file is a JSON array that is appended to throughout the day.
New file created automatically each trading day.

Usage
-----
    logger = AnalyticsLogger()
    logger.log_decision(signal_dict)    # Every 5-min signal scan
    logger.log_trade(trade_record)      # When a trade closes
"""

import json
import logging
import os
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict

from config.settings import DECISIONS_DIR, TRADES_DIR

log = logging.getLogger(__name__)


# =============================================================================
# ANALYTICS LOGGER
# =============================================================================

class AnalyticsLogger:
    """
    Appends signal decisions and trade outcomes to daily JSON files.

    Thread-safe: each write is an atomic file read-append-write.
    """

    def __init__(self) -> None:
        DECISIONS_DIR.mkdir(parents=True, exist_ok=True)
        TRADES_DIR.mkdir(parents=True, exist_ok=True)
        log.info("[AnalyticsLogger] Initialised | decisions=%s trades=%s",
                 DECISIONS_DIR, TRADES_DIR)

    def load_today_trades(self) -> list:
        """
        Load today closed trade records from trades/YYYY-MM-DD.json.
        Returns an empty list if no trades today or file does not exist.
        Used by /pnl Telegram command.
        """
        today_file = TRADES_DIR / f"{date.today().isoformat()}.json"
        if not today_file.exists():
            return []
        try:
            data = json.loads(today_file.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception as exc:
            log.warning("[AnalyticsLogger] load_today_trades failed: %s", exc)
            return []

    # =========================================================================
    # SIGNAL DECISION LOG
    # =========================================================================

    def log_decision(self, signal: Dict) -> None:
        """
        Log one signal computation result to decisions/YYYY-MM-DD.json.

        Called by engine.py after every SignalEngine.compute() call,
        regardless of whether a trade was opened.

        Parameters
        ----------
        signal : dict from SignalEngine.compute()
        """
        entry = {
            "timestamp":         _now_iso(),
            "is_trade":          signal.get("is_trade", False),
            "direction":         signal.get("direction", "SIDEWAYS"),
            "trade_signal":      signal.get("trade_signal", "NO TRADE"),
            "adjusted_score":    signal.get("adjusted_score", 0),
            "base_score":        signal.get("base_score", 0),
            "confidence_level":  signal.get("confidence_level", "LOW"),
            "alignment_summary": signal.get("alignment_summary", "N/A"),
            "price":             signal.get("price"),
            "vwap":              signal.get("vwap"),
            "ema9":              signal.get("ema9"),
            "oi_result":         signal.get("oi_result", {}),
            "sent_result":       signal.get("sent_result", {}),
            "scale_up":          signal.get("scale_up", False),
            "_reason":           signal.get("_reason", ""),
        }
        _append(DECISIONS_DIR / _today_filename(), entry)
        log.debug(
            "[AnalyticsLogger] Decision logged | %s score=%d",
            entry["trade_signal"], entry["adjusted_score"],
        )

    # =========================================================================
    # TRADE OUTCOME LOG
    # =========================================================================

    def log_trade(self, trade: Any) -> None:
        """
        Log one completed trade to trades/YYYY-MM-DD.json.

        Called by engine.py after a trade closes (SL / reversal / EOD).

        Parameters
        ----------
        trade : TradeRecord from TradeManager.get_trade()
        """
        # Build milestone breakdown dict
        milestone_prices = trade.target_sequence if hasattr(trade, "target_sequence") else {}

        entry = {
            "trade_id":        trade.trade_id,
            "open_time":       trade.open_time,
            "close_time":      trade.close_time,
            "close_reason":    trade.close_reason,
            "status":          trade.status,
            "direction":       trade.direction,
            "signal_text":     trade.signal_text,
            "confidence":      trade.confidence,
            "lots":            trade.lots,
            "entry_price":     trade.entry_price,
            "close_price":     trade.current_price,
            "initial_sl":      trade.initial_sl,
            "final_sl":        trade.sl_price,
            "last_milestone":  trade.last_milestone,
            "milestones_hit":  trade.milestones_hit,
            "milestone_prices":milestone_prices,
            "pnl_points":      trade.pnl_points,
            "pnl_inr":         trade.pnl_inr,
        }
        _append(TRADES_DIR / _today_filename(), entry)
        log.info(
            "[AnalyticsLogger] Trade logged | id=%s pnl=%.1fpts ₹%.0f",
            trade.trade_id, trade.pnl_points, trade.pnl_inr,
        )


# =============================================================================
# HELPERS
# =============================================================================

def _today_filename() -> str:
    """Returns 'YYYY-MM-DD.json' for today."""
    return date.today().strftime("%Y-%m-%d") + ".json"


def _now_iso() -> str:
    return datetime.now().isoformat()


def _append(filepath: Path, entry: Dict) -> None:
    """
    Thread-safe append to a JSON array file.
    Creates the file with an empty array if it doesn't exist.
    """
    try:
        if filepath.exists():
            existing = json.loads(filepath.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                existing = []
        else:
            existing = []

        existing.append(entry)

        # Atomic write
        tmp = filepath.with_suffix(".tmp")
        tmp.write_text(json.dumps(existing, indent=2, default=str), encoding="utf-8")
        tmp.replace(filepath)

    except Exception as exc:
        log.error("[AnalyticsLogger] Failed to write %s: %s", filepath, exc)
