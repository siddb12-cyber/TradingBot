"""
analytics/trade_summary.py
===========================
Comprehensive trade analytics engine for the TradingBot system.

Reads real closed trade outcomes from daily Excel logs — Outcome and
Points_Result columns are now populated by the Trade State Machine
(Priority 2 upgrade). Analytics are no longer fabricated.

Features:
    - Per-trade breakdown table (entry, exit, outcome, P&L)
    - Win rate (targets hit vs SL hit)
    - Total points captured / lost
    - Best and worst trade
    - Open trade detection (not yet closed)
    - Multi-day support: run for a specific date or a date range

Usage:
    # Run for today:
    python -m analytics.trade_summary

    # Run for a specific date:
    python -m analytics.trade_summary --date 2026-05-15

    # Run for a date range:
    python -m analytics.trade_summary --from 2026-05-01 --to 2026-05-15
"""

import argparse
import logging
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from config.config import configure_logging, TRADE_LOG_DIR

# =========================
# LOGGING
# =========================

configure_logging()
logger = logging.getLogger(__name__)

# =========================
# CONSTANTS
# =========================

WIN_OUTCOMES  = {"TARGET 3 HIT", "TARGET 2 HIT", "TARGET 1 HIT"}
LOSS_OUTCOMES = {"SL HIT"}
OPEN_STATUSES = {"OPEN", "TARGET1_HIT", "TARGET2_HIT"}

OUTCOME_POINT_MAP = {
    "TARGET 3 HIT": "+40 pts",
    "TARGET 2 HIT": "+25 pts",
    "TARGET 1 HIT": "+15 pts",
    "SL HIT":       "-10 pts",
}


# =========================
# DATA LOADING
# =========================

def load_log_for_date(target_date: str) -> Optional[pd.DataFrame]:
    """
    Load trade log for a specific date string (YYYY-MM-DD).
    Returns None if the file is missing or unreadable.
    """
    log_file = TRADE_LOG_DIR / f"trade_log_{target_date}.xlsx"

    if not log_file.exists():
        logger.debug(f"[SUMMARY] No log file for {target_date}")
        return None

    try:
        df = pd.read_excel(log_file, engine="openpyxl")
        if df.empty:
            return None
        df["_source_date"] = target_date
        return df
    except Exception as exc:
        logger.error(f"[SUMMARY] Failed to read {log_file.name}: {exc}")
        return None


def load_logs_for_range(date_from: str, date_to: str) -> pd.DataFrame:
    """
    Load and concatenate trade logs for a range of dates.
    Silently skips dates with no log file.
    """
    frames = []
    start  = datetime.strptime(date_from, "%Y-%m-%d").date()
    end    = datetime.strptime(date_to,   "%Y-%m-%d").date()
    current = start

    while current <= end:
        df = load_log_for_date(current.strftime("%Y-%m-%d"))
        if df is not None:
            frames.append(df)
        current += timedelta(days=1)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# =========================
# ANALYSIS ENGINE
# =========================

def analyse(df: pd.DataFrame, label: str = "Period") -> None:
    """
    Compute and print a full performance summary from a trade log DataFrame.

    Handles:
    - DataFrames without Outcome/Points Result columns (older logs)
    - Mixed DataFrames with open + closed trades
    - Correct separation of NO TRADE scan rows from actual trade rows
    """
    print(f"\n{'═' * 52}")
    print(f"  TRADE SUMMARY — {label.upper()}")
    print(f"{'═' * 52}")

    if df.empty:
        print("  No data found for this period.")
        print(f"{'═' * 52}\n")
        return

    # --- Separate scan rows (NO TRADE) from actual trade rows ---
    no_trade_mask = (
        df["Trade Signal"].isna() |
        (df["Trade Signal"] == "NO TRADE")
    )
    scan_df    = df[no_trade_mask]
    trade_df   = df[~no_trade_mask].copy()

    total_scans  = len(df)
    no_trade_cnt = len(scan_df)
    total_trades = len(trade_df)

    print(f"\n  {'Total scan cycles':<28}: {total_scans}")
    print(f"  {'Signals generated':<28}: {total_trades}")
    print(f"  {'No-trade (sideways)':<28}: {no_trade_cnt}")

    if total_trades == 0:
        print("\n  No trade signals generated in this period.")
        print(f"{'═' * 52}\n")
        return

    # --- Directional breakdown ---
    ce_count = int(trade_df["Trade Signal"].str.contains("CE", na=False).sum())
    pe_count = int(trade_df["Trade Signal"].str.contains("PE", na=False).sum())
    print(f"  {'CE (bullish) signals':<28}: {ce_count}")
    print(f"  {'PE (bearish) signals':<28}: {pe_count}")

    # --- Check if state machine columns are present ---
    has_outcome       = "Outcome" in trade_df.columns
    has_points_result = "Points Result" in trade_df.columns
    has_trade_status  = "Trade Status" in trade_df.columns

    if not has_outcome:
        print(
            "\n  ⚠️  Outcome column missing — Trade State Machine not yet active.\n"
            "     Upgrade to Priority 2 to get real P&L data."
        )
        print(f"{'═' * 52}\n")
        return

    # --- Classify trades ---
    closed_mask = trade_df["Outcome"].notna() & (trade_df["Outcome"] != "")
    open_mask   = ~closed_mask

    closed_df = trade_df[closed_mask]
    open_df   = trade_df[open_mask]

    closed_count = len(closed_df)
    open_count   = len(open_df)

    print(f"\n  {'Closed trades':<28}: {closed_count}")
    print(f"  {'Open / pending':<28}: {open_count}")

    if open_count > 0:
        print(f"\n  ⚡ OPEN TRADE(S) DETECTED:")
        for _, row in open_df.iterrows():
            trade_id = row.get("Trade ID", "N/A")
            signal   = row.get("Trade Signal", "N/A")
            entry    = row.get("Current Price", "N/A")
            status   = row.get("Trade Status", "OPEN")
            print(f"     → {signal}  |  Entry: {entry}  |  ID: {trade_id}  |  Status: {status}")

    if closed_count == 0:
        print("\n  No closed trades yet in this period.")
        print(f"{'═' * 52}\n")
        return

    # --- Outcome distribution ---
    print(f"\n  {'─' * 48}")
    print(f"  CLOSED TRADE OUTCOMES:")
    outcome_counts = closed_df["Outcome"].value_counts()
    for outcome, cnt in outcome_counts.items():
        pts_hint = OUTCOME_POINT_MAP.get(outcome, "")
        print(f"    {outcome:<22}: {cnt}  {pts_hint}")

    # --- Win / Loss rate ---
    wins   = int(closed_df["Outcome"].isin(WIN_OUTCOMES).sum())
    losses = int(closed_df["Outcome"].isin(LOSS_OUTCOMES).sum())
    total_decided = wins + losses

    if total_decided > 0:
        win_rate = (wins / total_decided) * 100
        print(f"\n  {'Win rate':<28}: {win_rate:.1f}%  ({wins}W / {losses}L)")
    else:
        print(f"\n  Win rate: N/A (no outcomes classified)")

    # --- Points P&L ---
    if has_points_result:
        pts_col  = pd.to_numeric(closed_df["Points Result"], errors="coerce")
        pts_total = pts_col.sum()
        pts_best  = pts_col.max()
        pts_worst = pts_col.min()

        print(f"\n  {'─' * 48}")
        print(f"  POINTS PERFORMANCE:")
        print(f"    {'Total points captured':<26}: {pts_total:+.2f}")
        print(f"    {'Best trade':<26}: {pts_best:+.2f}")
        print(f"    {'Worst trade':<26}: {pts_worst:+.2f}")

        if closed_count > 0:
            avg_pts = pts_total / closed_count
            print(f"    {'Average per trade':<26}: {avg_pts:+.2f}")

    # --- Per-trade table ---
    if closed_count > 0:
        print(f"\n  {'─' * 48}")
        print("  TRADE-BY-TRADE DETAIL:")

        display_cols = ["Date", "Time", "Trade Signal", "Current Price",
                        "Trade Status", "Outcome", "Points Result",
                        "Exit Price", "Exit Time"]

        available_cols = [c for c in display_cols if c in closed_df.columns]
        display_df     = closed_df[available_cols].copy()

        # Align column widths for console output
        pd.set_option("display.max_columns", None)
        pd.set_option("display.width", 140)
        pd.set_option("display.float_format", lambda x: f"{x:.2f}")

        table_str = display_df.to_string(index=False)
        for line in table_str.split("\n"):
            print(f"  {line}")

    print(f"\n{'═' * 52}\n")


# =========================
# ENTRY POINT
# =========================

def run(
    date_str:  Optional[str] = None,
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
) -> None:
    """
    Load and analyse trade logs for the specified date or range.

    Args:
        date_str:  single date "YYYY-MM-DD" (defaults to today)
        date_from: start of range "YYYY-MM-DD"
        date_to:   end of range "YYYY-MM-DD" (defaults to today if date_from set)
    """
    today = datetime.now().strftime("%Y-%m-%d")

    if date_from is not None:
        # Date range mode
        date_to = date_to or today
        label   = f"{date_from} to {date_to}"
        df      = load_logs_for_range(date_from, date_to)
    else:
        # Single date mode (default: today)
        target = date_str or today
        label  = target
        df     = load_log_for_date(target) or pd.DataFrame()

    analyse(df, label=label)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="TradingBot trade summary analytics"
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Single date to analyse (YYYY-MM-DD). Defaults to today.",
    )
    parser.add_argument(
        "--from",
        dest="date_from",
        type=str,
        default=None,
        help="Start date for range analysis (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--to",
        dest="date_to",
        type=str,
        default=None,
        help="End date for range analysis (YYYY-MM-DD). Defaults to today.",
    )

    args = parser.parse_args()

    run(
        date_str=  args.date,
        date_from= args.date_from,
        date_to=   args.date_to,
    )
