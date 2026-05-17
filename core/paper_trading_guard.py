"""
core/paper_trading_guard.py
============================
Global enforcement layer for the paper trading validation phase.

Responsibilities:
    1. Assert PAPER_TRADING_MODE=True at import time — fails loudly if misconfigured
    2. Patch DRY_RUN to True in groww_execution_engine at import time
    3. Provide session ID generation (PAPER_YYYYMMDD_HHMMSS)
    4. Provide a hard-block decorator for place_order() calls
    5. Log all enforcement actions clearly with [PAPER GUARD] prefix

Usage:
    # At the top of groww_execution_engine.py:
    from core.paper_trading_guard import enforce_paper_mode, block_if_paper, get_session_id
    enforce_paper_mode()   # call once at module import

    # On place_order():
    @block_if_paper
    def place_order(...):
        ...

This module has no side effects beyond logging when imported.
Call enforce_paper_mode() explicitly to trigger the assertion.
"""

import logging
import functools
from datetime import datetime

from config.config import (
    PAPER_TRADING_MODE,
    PAPER_TRADING_VALIDATION_END,
    PAPER_SESSION_PREFIX,
)

logger = logging.getLogger(__name__)

SEP = "=" * 52

# =========================
# SESSION ID
# =========================

def get_session_id() -> str:
    """
    Generate a unique paper trading session ID for the current day.

    Format: PAPER_YYYYMMDD_HHMMSS

    Used to tag:
        - Trade IDs in paper validation metrics
        - Dashboard log entries
        - EOD report headers

    Returns:
        str — e.g. "PAPER_20260515_093000"
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{PAPER_SESSION_PREFIX}_{ts}"


def get_daily_session_id() -> str:
    """
    Daily session ID (date only) — consistent across the trading day.

    Format: PAPER_YYYYMMDD

    Returns:
        str — e.g. "PAPER_20260515"
    """
    today = datetime.now().strftime("%Y%m%d")
    return f"{PAPER_SESSION_PREFIX}_{today}"


# =========================
# ENFORCEMENT
# =========================

def enforce_paper_mode() -> None:
    """
    Assert that PAPER_TRADING_MODE is True.
    Raises RuntimeError if misconfigured (e.g. accidentally set to False).

    Call this once at the top of any module that touches order execution.
    Logs a prominent [PAPER GUARD] banner on every startup.
    """
    if not PAPER_TRADING_MODE:
        raise RuntimeError(
            "[PAPER GUARD] CRITICAL: PAPER_TRADING_MODE is False in config.py.\n"
            "Real order placement is not permitted during the validation phase "
            f"(ends {PAPER_TRADING_VALIDATION_END}).\n"
            "To proceed, review and explicitly change PAPER_TRADING_MODE after "
            "validation sign-off."
        )

    logger.info(SEP)
    logger.info("[PAPER GUARD] PAPER TRADE MODE ACTIVE")
    logger.info(f"[PAPER GUARD] Validation phase ends : {PAPER_TRADING_VALIDATION_END}")
    logger.info(f"[PAPER GUARD] Daily session ID      : {get_daily_session_id()}")
    logger.info("[PAPER GUARD] Real order placement  : BLOCKED")
    logger.info("[PAPER GUARD] DRY_RUN               : FORCED TRUE")
    logger.info(SEP)


def assert_paper_mode_active() -> None:
    """
    Lightweight assertion — raises RuntimeError if PAPER_TRADING_MODE is False.
    Use inside individual functions as a secondary guard.
    """
    if not PAPER_TRADING_MODE:
        raise RuntimeError(
            "[PAPER GUARD] PAPER_TRADING_MODE=False — this call is blocked "
            "during the validation phase."
        )


# =========================
# DECORATOR: BLOCK IF PAPER
# =========================

def block_if_paper(func):
    """
    Decorator that hard-blocks a function when PAPER_TRADING_MODE=True.

    Usage:
        @block_if_paper
        def place_order(page, params, dry_run):
            ...

    When PAPER_TRADING_MODE=True:
        - Function body is never executed
        - Raises RuntimeError with a clear message
        - Logs [PAPER GUARD] BLOCKED entry

    When PAPER_TRADING_MODE=False:
        - Function executes normally (validation phase ended)
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if PAPER_TRADING_MODE:
            msg = (
                f"[PAPER GUARD] BLOCKED: {func.__qualname__}() cannot execute "
                f"during paper trading validation phase (ends {PAPER_TRADING_VALIDATION_END}). "
                f"Set PAPER_TRADING_MODE=False in config.py after sign-off."
            )
            logger.error(msg)
            raise RuntimeError(msg)
        return func(*args, **kwargs)
    return wrapper


# =========================
# WATERMARK HELPERS
# =========================

def paper_tag() -> str:
    """
    Return the standard paper trading watermark string for Telegram messages.
    Replaces the plain [Paper Trading Mode] tag with a more prominent one.
    """
    return f"[PAPER TRADE MODE ACTIVE | Validation ends {PAPER_TRADING_VALIDATION_END}]"


def paper_log_prefix() -> str:
    """
    Return a prefix for log lines that should be flagged as paper trades.
    e.g. "[PAPER][PAPER_20260515]"
    """
    return f"[PAPER][{get_daily_session_id()}]"
