"""
core/market_hours.py
====================
NSE market hours guard for the TradingBot system.

All time comparisons assume the system clock is set to IST (UTC+5:30).
No external timezone library is required — keep dependencies minimal.

Public API:
    is_market_open()           → bool   True if inside NSE trading hours
    is_eod_close_time()        → bool   True at 15:29 IST (auto-close window)
    is_trading_day()           → bool   True if today is Mon–Fri
    next_market_open_dt()      → datetime  Next 09:15 on a trading day
    seconds_until_next_open()  → int    Seconds to sleep until next open
    log_market_closed_reason() → None   Logs why market is closed + wait time

Usage:
    from core.market_hours import is_market_open, is_eod_close_time
    from core.market_hours import seconds_until_next_open, log_market_closed_reason

    if not is_market_open():
        log_market_closed_reason()
        time.sleep(min(seconds_until_next_open(), 300))
        continue
"""

import logging
from datetime import datetime, timedelta

from config.settings import (
    MARKET_OPEN_HOUR, MARKET_OPEN_MINUTE,
    MARKET_CLOSE_HOUR, MARKET_CLOSE_MINUTE,
    EOD_CLOSE_HOUR, EOD_CLOSE_MINUTE,
)

# =========================
# LOGGING
# =========================

logger = logging.getLogger(__name__)

# =========================
# CONSTANTS
# =========================

# weekday() values: Monday=0 … Friday=4
_TRADING_DAYS: frozenset = frozenset({0, 1, 2, 3, 4})


# =========================
# INTERNAL HELPERS
# =========================

def _at_open(dt: datetime) -> datetime:
    """Return dt with time set to market open (09:15:00)."""
    return dt.replace(
        hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MINUTE,
        second=0, microsecond=0
    )


def _at_close(dt: datetime) -> datetime:
    """Return dt with time set to market close (15:30:00)."""
    return dt.replace(
        hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MINUTE,
        second=0, microsecond=0
    )


# =========================
# PUBLIC API
# =========================

def is_trading_day(dt: datetime = None) -> bool:
    """
    Returns True if the given datetime falls on a weekday (Mon–Fri).
    Does NOT account for NSE holidays — add a holiday list if needed.
    """
    dt = dt or datetime.now()
    return dt.weekday() in _TRADING_DAYS


def is_market_open(dt: datetime = None) -> bool:
    """
    Returns True if currently inside NSE trading hours.

    Condition: is_trading_day() AND 09:15:00 <= now < 15:30:00 (IST)

    Args:
        dt: datetime to test (defaults to now). Assumes IST.

    Returns:
        bool
    """
    dt = dt or datetime.now()

    if dt.weekday() not in _TRADING_DAYS:
        return False

    return _at_open(dt) <= dt < _at_close(dt)


def is_eod_close_time(dt: datetime = None) -> bool:
    """
    Returns True during the EOD auto-close window.

    Window: 15:29:00–15:29:59 IST on a trading day.
    The tracker fires once per tick (60s) so this window is hit reliably.

    Args:
        dt: datetime to test (defaults to now). Assumes IST.

    Returns:
        bool
    """
    dt = dt or datetime.now()

    if dt.weekday() not in _TRADING_DAYS:
        return False

    return dt.hour == EOD_CLOSE_HOUR and dt.minute == EOD_CLOSE_MINUTE


def next_market_open_dt(dt: datetime = None) -> datetime:
    """
    Returns the datetime of the next NSE market open (09:15 AM IST).

    Logic:
        - If today is a trading day and we're before 09:15 → today 09:15
        - Otherwise advance day-by-day until next weekday, return 09:15 of that day

    Args:
        dt: reference datetime (defaults to now). Assumes IST.

    Returns:
        datetime — next 09:15 on a trading day
    """
    dt = dt or datetime.now()

    # If today is a trading day and market hasn't opened yet
    if dt.weekday() in _TRADING_DAYS and dt < _at_open(dt):
        return _at_open(dt)

    # Advance to next calendar day, skip weekends
    candidate = dt.date() + timedelta(days=1)
    while candidate.weekday() not in _TRADING_DAYS:
        candidate += timedelta(days=1)

    return datetime(
        candidate.year, candidate.month, candidate.day,
        MARKET_OPEN_HOUR, MARKET_OPEN_MINUTE, 0
    )


def seconds_until_next_open(dt: datetime = None) -> int:
    """
    Returns integer seconds until the next NSE market open.

    Minimum return value is 60 to prevent zero-sleep tight loops.

    Args:
        dt: reference datetime (defaults to now). Assumes IST.

    Returns:
        int — seconds to sleep
    """
    dt        = dt or datetime.now()
    next_open = next_market_open_dt(dt)
    delta     = (next_open - dt).total_seconds()
    return max(int(delta), 60)


def log_market_closed_reason(dt: datetime = None) -> None:
    """
    Log a clear, human-readable message explaining why the market is closed
    and how long until it opens.

    Args:
        dt: reference datetime (defaults to now). Assumes IST.
    """
    dt       = dt or datetime.now()
    nxt      = next_market_open_dt(dt)
    secs     = seconds_until_next_open(dt)
    hrs, rem = divmod(secs, 3600)
    mins     = rem // 60

    # Determine reason
    if dt.weekday() not in _TRADING_DAYS:
        day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                     "Saturday", "Sunday"]
        reason = f"Weekend ({day_names[dt.weekday()]})"
    elif dt < _at_open(dt):
        reason = "Pre-market (before 09:15 IST)"
    else:
        reason = "After-hours (market closed at 15:30 IST)"

    logger.info(
        f"[MARKET] CLOSED — {reason} | "
        f"Next open: {nxt.strftime('%a %Y-%m-%d %H:%M IST')} | "
        f"Wait: {hrs}h {mins}m"
    )
