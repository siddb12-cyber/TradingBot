"""
analytics/trade_archiver.py
============================
Daily trade archiver for TradingBot.

Writes one JSON file per trading day to data/archive/YYYY-MM-DD.json.
Also maintains data/archive/index.json — a master list of all archived dates
with their headline stats, used by the analytics dashboard for fast lookups.

Archive schema (per day file):
  {
    "date":         "2026-05-22",
    "session_mode": "PAPER",
    "trades":       [...],          # Full trade records
    "daily_summary": {
        "trades_count",  "wins", "losses", "breakeven",
        "net_pnl_pts",   "net_pnl_inr",
        "gross_profit_pts", "gross_loss_pts",
        "win_rate",      "profit_factor",
        "expectancy_pts","avg_r_multiple",
        "max_win_pts",   "max_loss_pts",
        "avg_holding_mins",
        "best_confidence_score",
        "avg_confidence_score",
    },
    "archived_at":  "2026-05-22T15:30:00"
  }

Usage:
    from analytics.trade_archiver import TradeArchiver
    archiver = TradeArchiver()
    archiver.archive_day(trades_list, risk_state_dict)
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from config.config import (
    BASE_DIR,
    DATA_DIR,
    NIFTY_LOT_SIZE,
    OPTION_DELTA,
    PAPER_TRADING_MODE,
)

logger = logging.getLogger(__name__)

ARCHIVE_DIR = DATA_DIR / "archive"


# ══════════════════════════════════════════════════════════════
# ARCHIVER
# ══════════════════════════════════════════════════════════════

class TradeArchiver:
    """
    Persists daily trade sessions to data/archive/YYYY-MM-DD.json.
    Also maintains a master index at data/archive/index.json.
    """

    def __init__(self):
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    # ─────────────────────────────────────────────────────────
    # PUBLIC: Archive one day
    # ─────────────────────────────────────────────────────────

    def archive_day(
        self,
        trades:     List[dict],
        risk_state: dict,
        date:       Optional[str] = None,
    ) -> Path:
        """
        Write today's trades + summary to archive file.
        Safe to call multiple times during the day (overwrites in place).

        Args:
            trades:     List of closed trade records (from trading_engine._closed_trades_today)
            risk_state: Current risk engine state dict
            date:       Override date string YYYY-MM-DD (defaults to today)

        Returns:
            Path to the written archive file.
        """
        today     = date or datetime.now().strftime("%Y-%m-%d")
        summary   = self._compute_summary(trades, risk_state)
        _pts_lot  = NIFTY_LOT_SIZE * OPTION_DELTA

        payload = {
            "date":         today,
            "session_mode": "PAPER" if PAPER_TRADING_MODE else "LIVE",
            "trades":       trades,
            "daily_summary": summary,
            "risk_state":   {
                "trades_today":           risk_state.get("trades_today", 0),
                "trades_won":             risk_state.get("trades_won", 0),
                "trades_lost":            risk_state.get("trades_lost", 0),
                "daily_pnl_points":       risk_state.get("daily_pnl_points", 0.0),
                "daily_pnl_inr":          round(risk_state.get("daily_pnl_points", 0.0) * _pts_lot, 2),
                "consecutive_wins":       risk_state.get("consecutive_wins", 0),
                "consecutive_losses":     risk_state.get("consecutive_losses", 0),
                "max_consecutive_wins":   risk_state.get("max_consecutive_wins", 0),
                "max_consecutive_losses": risk_state.get("max_consecutive_losses", 0),
                "trade_limit_extension":  risk_state.get("trade_limit_extension", 0),
            },
            "archived_at": datetime.now().isoformat(timespec="seconds"),
        }

        out_path = ARCHIVE_DIR / f"{today}.json"
        tmp_path = ARCHIVE_DIR / f"{today}.tmp"

        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
        os.replace(tmp_path, out_path)

        logger.info("[Archiver] Day archived: %s (%d trades, net=%.2f pts)",
                    today, len(trades), summary.get("net_pnl_pts", 0))

        # Update the master index
        self._update_index(today, summary)

        return out_path

    # ─────────────────────────────────────────────────────────
    # PUBLIC: Read archive
    # ─────────────────────────────────────────────────────────

    def get_day(self, date: str) -> Optional[dict]:
        """Load archive for a specific date. Returns None if not found."""
        path = ARCHIVE_DIR / f"{date}.json"
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            logger.warning("[Archiver] Could not read %s: %s", path, exc)
            return None

    def get_index(self) -> List[dict]:
        """Return the master index list (newest first)."""
        path = ARCHIVE_DIR / "index.json"
        if not path.exists():
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return sorted(data.get("days", []), key=lambda x: x["date"], reverse=True)
        except Exception as exc:
            logger.warning("[Archiver] Could not read index: %s", exc)
            return []

    def list_dates(self) -> List[str]:
        """Return all archived dates as YYYY-MM-DD strings, newest first."""
        return [d["date"] for d in self.get_index()]

    # ─────────────────────────────────────────────────────────
    # PRIVATE: Summary computation
    # ─────────────────────────────────────────────────────────

    def _compute_summary(self, trades: List[dict], risk_state: dict) -> dict:
        """Compute comprehensive daily stats from trade list."""
        _pts_lot = NIFTY_LOT_SIZE * OPTION_DELTA

        count      = len(trades)
        wins       = [t for t in trades if t.get("pts_pnl", 0) > 0]
        losses     = [t for t in trades if t.get("pts_pnl", 0) < 0]
        breakeven  = [t for t in trades if t.get("pts_pnl", 0) == 0]

        gross_profit = sum(t.get("pts_pnl", 0) for t in wins)
        gross_loss   = sum(t.get("pts_pnl", 0) for t in losses)   # negative
        net_pnl_pts  = round(gross_profit + gross_loss, 2)
        net_pnl_inr  = round(net_pnl_pts * _pts_lot, 2)

        win_rate     = round(len(wins) / count * 100, 1) if count > 0 else 0.0
        loss_rate    = 1 - win_rate / 100
        profit_factor= round(gross_profit / abs(gross_loss), 2) if gross_loss != 0 else None

        avg_win      = round(gross_profit / len(wins), 2) if wins else 0.0
        avg_loss     = round(abs(gross_loss) / len(losses), 2) if losses else 0.0
        # Expectancy: (win_rate × avg_win) - (loss_rate × avg_loss)
        expectancy   = round((win_rate / 100) * avg_win - loss_rate * avg_loss, 2)

        # R-multiple: each trade's P&L as multiple of SL risk
        from config.config import STOP_LOSS_POINTS
        r_multiples  = [round(t.get("pts_pnl", 0) / STOP_LOSS_POINTS, 2) for t in trades if STOP_LOSS_POINTS > 0]
        avg_r        = round(sum(r_multiples) / len(r_multiples), 2) if r_multiples else 0.0

        max_win_pts  = max((t.get("pts_pnl", 0) for t in trades), default=0)
        max_loss_pts = min((t.get("pts_pnl", 0) for t in trades), default=0)

        # Holding time (minutes between entry_time and exit_time)
        holding_times = []
        for t in trades:
            try:
                entry = datetime.strptime(t["entry_time"], "%H:%M:%S")
                exit_ = datetime.strptime(t["exit_time"],  "%H:%M:%S")
                mins  = int((exit_ - entry).total_seconds() / 60)
                if mins >= 0:
                    holding_times.append(mins)
            except Exception:
                pass
        avg_holding  = round(sum(holding_times) / len(holding_times), 1) if holding_times else 0

        return {
            "trades_count":     count,
            "wins":             len(wins),
            "losses":           len(losses),
            "breakeven":        len(breakeven),
            "net_pnl_pts":      net_pnl_pts,
            "net_pnl_inr":      net_pnl_inr,
            "gross_profit_pts": round(gross_profit, 2),
            "gross_loss_pts":   round(gross_loss, 2),
            "win_rate":         win_rate,
            "profit_factor":    profit_factor,
            "expectancy_pts":   expectancy,
            "avg_r_multiple":   avg_r,
            "max_win_pts":      max_win_pts,
            "max_loss_pts":     max_loss_pts,
            "avg_holding_mins": avg_holding,
        }

    def _update_index(self, date: str, summary: dict) -> None:
        """Upsert this day's headline entry into data/archive/index.json."""
        index_path = ARCHIVE_DIR / "index.json"
        try:
            if index_path.exists():
                with open(index_path, "r", encoding="utf-8") as f:
                    idx = json.load(f)
            else:
                idx = {"days": []}

            # Remove existing entry for this date (upsert)
            idx["days"] = [d for d in idx["days"] if d["date"] != date]
            idx["days"].append({
                "date":         date,
                "trades_count": summary.get("trades_count", 0),
                "wins":         summary.get("wins", 0),
                "losses":       summary.get("losses", 0),
                "net_pnl_pts":  summary.get("net_pnl_pts", 0.0),
                "net_pnl_inr":  summary.get("net_pnl_inr", 0.0),
                "win_rate":     summary.get("win_rate", 0.0),
                "profit_factor":summary.get("profit_factor"),
                "expectancy_pts":summary.get("expectancy_pts", 0.0),
            })

            tmp = ARCHIVE_DIR / "index.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(idx, f, indent=2, default=str)
            os.replace(tmp, index_path)

        except Exception as exc:
            logger.warning("[Archiver] Index update failed: %s", exc)
