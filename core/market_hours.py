"""
core/market_hours.py
====================
NSE market hours guard for the TradingBot system.

All time comparisons assume the system clock is set to IST (UTC+5:30).
No external timezone library is required — keep dependencies minimal.

Public API:
    is_market_open()           → bool   True if inside NSE trading hours
    is_eod_close_time()        → bool   True at 15:29 IST (auto-close window)
    is_trading_day()           → bool   True if today is Mon–Fri (excl. NSE holidays)
    next_market_open_dt()      → datetime  Next 09:15 on a trading day (holiday-aware)
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
from datetime import datetime, date, timedelta

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
# NSE HOLIDAY CALENDAR
# Source: NSE India official holiday list
# Update this list each year in January.
# =========================

_NSE_HOLIDAYS: frozenset = frozenset({
    # ── 2025 ──────────────────────────────────────────────────────────────────
    date(2025, 1, 26),   # Republic Day
    date(2025, 2, 26),   # Mahashivratri
    date(2025, 3, 14),   # Holi
    date(2025, 3, 31),   # Id-Ul-Fitr (Ramadan Eid)
    date(2025, 4, 10),   # Shri Ram Navami
    date(2025, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
    date(2025, 4, 18),   # Good Friday
    date(2025, 5, 1),    # Maharashtra Day
    date(2025, 8, 15),   # Independence Day
    date(2025, 8, 27),   # Ganesh Chaturthi
    date(2025, 10, 2),   # Mahatma Gandhi Jayanti / Dussehra
    date(2025, 10, 20),  # Diwali - Laxmi Pujan (Muhurat Trading — special session)
    date(2025, 10, 21),  # Diwali - Balipratipada
    date(2025, 11, 5),   # Prakash Gurpurab Sri Guru Nanak Dev Ji
    date(2025, 12, 25),  # Christmas

    # ── 2026 ──────────────────────────────────────────────────────────────────
    date(2026, 1, 26),   # Republic Day
    date(2026, 2, 26),   # Mahashivratri (tentative — verify against NSE circular)
    date(2026, 3, 20),   # Holi (tentative)
    date(2026, 3, 20),   # Holi
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
    date(2026, 4, 17),   # Shri Ram Navami (tentative)
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 5, 27),   # Buddha Purnima / Bank Holiday (NSE closed)
    date(2026, 8, 15),   # Independence Day
    date(2026, 10, 2),   # Mahatma Gandhi Jayanti
    date(2026, 11, 11),  # Diwali - Laxmi Pujan (tentative — verify)
    date(2026, 12, 25),  # Christmas

    # Add further 2026 dates as NSE publishes the official circular.
    # Reference: https://www.nseindia.com/products-services/equity-market-trading-holidays
})


def is_nse_holiday(dt: datetime = None) -> bool:
    """Returns True if the given date is an NSE-declared holiday."""
    d = (dt or datetime.now()).date()
    return d in _NSE_HOLIDAYS


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
    Returns True if the given datetime is a valid NSE trading day.
    Checks: weekday (Mon–Fri) AND not in NSE holiday calendar.
    """
    dt = dt or datetime.now()
    if dt.weekday() not in _TRADING_DAYS:
        return False
    return not is_nse_holiday(dt)


def is_market_open(dt: datetime = None) -> bool:
    """
    Returns True if currently inside NSE trading hours.

    Conditions:
      1. is_trading_day() → weekday Mon-Fri AND not an NSE holiday
      2. 09:15:00 <= now < 15:30:00 (IST)

    Args:
        dt: datetime to test (defaults to now). Assumes IST.

    Returns:
        bool
    """
    dt = dt or datetime.now()

    if not is_trading_day(dt):
        return False

    return _at_open(dt) <= dt < _at_close(dt)


def is_eod_close_time(dt: datetime = None) -> bool:
    """
    Returns True during the EOD auto-close window.

    Window: 15:29:00–15:29:59 IST on a trading day.
    The tracker fires once per tick (30s) so this window is hit reliably.

    Args:
        dt: datetime to test (defaults to now). Assumes IST.

    Returns:
        bool
    """
    dt = dt or datetime.now()

    if not is_trading_day(dt):
        return False

    return dt.hour == EOD_CLOSE_HOUR and dt.minute == EOD_CLOSE_MINUTE


def next_market_open_dt(dt: datetime = None) -> datetime:
    """
    Returns the datetime of the next NSE market open (09:15 AM IST).

    Logic:
        - If today is a trading day and we're before 09:15 → today 09:15
        - Otherwise advance day-by-day skipping weekends + NSE holidays

    Args:
        dt: reference datetime (defaults to now). Assumes IST.

    Returns:
        datetime — next 09:15 on a valid trading day
    """
    dt = dt or datetime.now()

    # If today is a trading day and market hasn't opened yet
    if is_trading_day(dt) and dt < _at_open(dt):
        return _at_open(dt)

    # Advance to next calendar day, skip weekends and holidays
    candidate = dt.date() + timedelta(days=1)
    while True:
        candidate_dt = datetime(candidate.year, candidate.month, candidate.day, 10, 0, 0)
        if is_trading_day(candidate_dt):
            break
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

    # Determine reason (holiday check first)
    if is_nse_holiday(dt):
        reason = f"NSE Holiday ({dt.strftime('%Y-%m-%d')})"
    elif dt.weekday() not in _TRADING_DAYS:
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
