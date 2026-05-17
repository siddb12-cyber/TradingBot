"""
analytics/daily_performance_report.py
======================================
End-of-day performance report for the TradingBot system.

Reads today's trade log and prints a full performance summary.
With the Trade State Machine (Priority 2) active, Outcome and Points_Result
columns are now populated with real data — no more fabricated numbers.

Delegates the heavy analysis to analytics.trade_summary.

Run manually at end of trading session:
    python -m analytics.daily_performance_report

For multi-day or date-range reports:
    python -m analytics.trade_summary --date 2026-05-15
    python -m analytics.trade_summary --from 2026-05-01 --to 2026-05-15
"""

import logging
from datetime import datetime

import pandas as pd

from config.config import configure_logging, TRADE_LOG_DIR
from analytics.trade_summary import load_log_for_date, analyse

# =========================
# LOGGING
# =========================

configure_logging()
logger = logging.getLogger(__name__)

# =========================
# ENTRY POINT
# =========================

def run() -> None:
    """
    Load today's trade log and print full performance summary.
    """
    today    = datetime.now().strftime("%Y-%m-%d")
    log_file = TRADE_LOG_DIR / f"trade_log_{today}.xlsx"

    if not log_file.exists():
        print(
            f"\n  No trade log found for today ({today}).\n"
            f"  Make sure ai_trading_assistant has run today.\n"
        )
        logger.warning(f"Trade log not found: {log_file}")
        return

    df = load_log_for_date(today)

    if df is None or df.empty:
        print(f"\n  Trade log for {today} is empty.\n")
        return

    # --- Show raw log table ---
    print(f"\n{'─' * 52}")
    print(f"  RAW TRADE LOG — {today}")
    print(f"{'─' * 52}")
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 160)
    pd.set_option("display.float_format", lambda x: f"{x:.2f}")
    print(df.drop(columns=["_source_date"], errors="ignore").to_string(index=False))

    # --- Delegate to trade_summary engine ---
    analyse(df, label=today)


if __name__ == "__main__":
    run()
