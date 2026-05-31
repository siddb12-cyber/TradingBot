"""
broker/order_manager.py
=======================
Routes trade orders through the PAPER_TRADING_MODE gate.

Paper mode  → logs the order details, returns a fake order_id, no HTTP calls
Live mode   → calls GrowwClient to place real orders on NSE F&O

This is the ONLY file that should call GrowwClient. trade_manager.py calls
this module; it never calls GrowwClient directly.

Order flow
----------
Entry:
    trade_manager.open_pending(signal)
        → order_manager.place_entry(signal, lots)
            → [PAPER] log + return fake_id
            → [LIVE]  groww_client.place_order(BUY, ...)

Exit (SL / target / EOD):
    trade_manager._close_trade(...)
        → order_manager.place_exit(trade)
            → [PAPER] log + return fake_id
            → [LIVE]  groww_client.place_order(SELL, ...)
"""

import logging
import time
from typing import Dict, Optional

from config.settings import (
    PAPER_TRADING_MODE,
    NIFTY_LOT_SIZE,
    GROWW_ORDER_TYPE,
    GROWW_PRODUCT,
)
from broker.groww_client import GrowwClient, GrowwAPIError, TRANSACTION_BUY, TRANSACTION_SELL

logger = logging.getLogger(__name__)

# Singleton client instance — created once, reused across all calls
_client: Optional[GrowwClient] = None


def _get_client() -> GrowwClient:
    """Return the singleton GrowwClient, creating it on first call."""
    global _client
    if _client is None:
        _client = GrowwClient()
        if not PAPER_TRADING_MODE:
            _client.authenticate()
    return _client


# ==============================================================================
# PUBLIC API
# ==============================================================================

def place_entry(signal: Dict, lots: int, expiry: str = "") -> Dict:
    """
    Place an entry order based on a signal dict from SignalEngine.compute().

    Parameters
    ----------
    signal : dict   — SignalEngine output (must have 'direction', 'strike', 'option_type')
    lots   : int    — number of lots (1 lot = NIFTY_LOT_SIZE = 75 qty)
    expiry : str    — option expiry string e.g. "29MAY2026" (nearest weekly)
                      If empty, order_manager will derive the nearest weekly expiry.

    Returns
    -------
    {
        "order_id":    str,    # real Groww order ID or "PAPER_<ts>" in paper mode
        "status":      str,    # "PAPER" | "PENDING" | "COMPLETE" | "REJECTED"
        "symbol":      str,
        "strike":      int,
        "option_type": str,
        "qty":         int,
        "transaction": str,    # "BUY"
        "mode":        str,    # "PAPER" | "LIVE"
        "error":       str,    # populated only on failure
    }
    """
    direction   = signal.get("direction", "")
    strike      = int(signal.get("strike", 0))
    option_type = signal.get("option_type", "CE" if direction == "BULLISH" else "PE")
    qty         = lots * NIFTY_LOT_SIZE

    if not expiry:
        expiry = _nearest_weekly_expiry()

    log_prefix = f"[OrderManager] ENTRY {'PAPER' if PAPER_TRADING_MODE else 'LIVE'}"

    # ---------- PAPER MODE ---------------------------------------------------
    if PAPER_TRADING_MODE:
        order_id = f"PAPER_{int(time.time())}"
        logger.info(
            "%s | BUY %d %s%s | qty=%d | expiry=%s | order_id=%s",
            log_prefix, strike, option_type, strike, qty, expiry, order_id,
        )
        return {
            "order_id":    order_id,
            "status":      "PAPER",
            "symbol":      "NIFTY",
            "strike":      strike,
            "option_type": option_type,
            "qty":         qty,
            "transaction": TRANSACTION_BUY,
            "mode":        "PAPER",
            "error":       "",
        }

    # ---------- LIVE MODE ----------------------------------------------------
    try:
        client = _get_client()
        result = client.place_order(
            symbol           = "NIFTY",
            strike           = strike,
            option_type      = option_type,
            expiry           = expiry,
            qty              = qty,
            transaction_type = TRANSACTION_BUY,
            order_type       = GROWW_ORDER_TYPE,
            product          = GROWW_PRODUCT,
            tag              = f"TBot_ENTRY_{direction[:4]}",
        )
        logger.info(
            "%s | order_id=%s status=%s",
            log_prefix, result.get("order_id"), result.get("status"),
        )
        return {**result, "mode": "LIVE", "error": ""}

    except GrowwAPIError as exc:
        logger.error("[OrderManager] ENTRY order FAILED: %s", exc)
        return {
            "order_id": "", "status": "FAILED",
            "symbol": "NIFTY", "strike": strike,
            "option_type": option_type, "qty": qty,
            "transaction": TRANSACTION_BUY, "mode": "LIVE",
            "error": str(exc),
        }


def place_exit(trade, reason: str = "SYSTEM") -> Dict:
    """
    Place a square-off (exit) order for an open trade.

    Parameters
    ----------
    trade  : TradeRecord — the current open trade from TradeManager
    reason : str         — "SL" | "TARGET" | "EOD" | "MANUAL" | "REVERSAL"

    Returns
    -------
    Same structure as place_entry() but transaction="SELL"
    """
    strike      = getattr(trade, "strike",      0)
    option_type = getattr(trade, "option_type", "CE")
    lots        = getattr(trade, "lots",        1)
    qty         = lots * NIFTY_LOT_SIZE
    expiry      = getattr(trade, "expiry",      _nearest_weekly_expiry())

    log_prefix = f"[OrderManager] EXIT({reason}) {'PAPER' if PAPER_TRADING_MODE else 'LIVE'}"

    # ---------- PAPER MODE ---------------------------------------------------
    if PAPER_TRADING_MODE:
        order_id = f"PAPER_EXIT_{int(time.time())}"
        logger.info(
            "%s | SELL %d %s | qty=%d | order_id=%s",
            log_prefix, strike, option_type, qty, order_id,
        )
        return {
            "order_id":    order_id,
            "status":      "PAPER",
            "symbol":      "NIFTY",
            "strike":      strike,
            "option_type": option_type,
            "qty":         qty,
            "transaction": TRANSACTION_SELL,
            "mode":        "PAPER",
            "error":       "",
        }

    # ---------- LIVE MODE ----------------------------------------------------
    try:
        client = _get_client()
        result = client.place_order(
            symbol           = "NIFTY",
            strike           = strike,
            option_type      = option_type,
            expiry           = expiry,
            qty              = qty,
            transaction_type = TRANSACTION_SELL,
            order_type       = GROWW_ORDER_TYPE,
            product          = GROWW_PRODUCT,
            tag              = f"TBot_EXIT_{reason[:6]}",
        )
        logger.info(
            "%s | order_id=%s status=%s",
            log_prefix, result.get("order_id"), result.get("status"),
        )
        return {**result, "mode": "LIVE", "error": ""}

    except GrowwAPIError as exc:
        logger.error("[OrderManager] EXIT order FAILED: %s", exc)
        return {
            "order_id": "", "status": "FAILED",
            "symbol": "NIFTY", "strike": strike,
            "option_type": option_type, "qty": qty,
            "transaction": TRANSACTION_SELL, "mode": "LIVE",
            "error": str(exc),
        }


def get_live_position(strike: int, option_type: str) -> Optional[Dict]:
    """
    Fetch current open position for a specific strike+type from Groww.
    Returns None in paper mode or if no matching position found.

    Used by tracker to cross-check paper state vs real broker state (live only).
    """
    if PAPER_TRADING_MODE:
        return None

    try:
        client    = _get_client()
        positions = client.get_positions()
        symbol    = f"NIFTY"  # simplified; real check uses full trading_symbol

        for pos in positions:
            sym = pos.get("trading_symbol", "")
            if str(strike) in sym and option_type in sym:
                return pos
        return None

    except Exception as exc:
        logger.warning("[OrderManager] get_live_position failed: %s", exc)
        return None


def get_available_margin() -> float:
    """
    Return available margin in INR. Returns 0.0 in paper mode.
    """
    if PAPER_TRADING_MODE:
        return 0.0

    try:
        funds = _get_client().get_funds()
        return float(funds.get("available_margin", 0.0))
    except Exception as exc:
        logger.warning("[OrderManager] get_available_margin failed: %s", exc)
        return 0.0


# ==============================================================================
# INTERNAL HELPERS
# ==============================================================================

def _nearest_weekly_expiry() -> str:
    """
    Derive the nearest NSE weekly expiry for NIFTY options.

    NIFTY weekly expiry = every Thursday.
    If today is Thursday after market hours, use next Thursday.

    Returns string in format "DDMONYYYY" e.g. "29MAY2026"

    TODO: Confirm exact expiry string format required by Groww API.
          Some brokers use "29-MAY-2026" or "2026-05-29" — verify with API docs.
    """
    from datetime import date, timedelta

    today    = date.today()
    weekday  = today.weekday()   # Monday=0 ... Sunday=6; Thursday=3

    # Days until next Thursday (or today if Thursday)
    days_to_thu = (3 - weekday) % 7
    if days_to_thu == 0:
        # Today is Thursday — market may have expired; use today
        days_to_thu = 0

    expiry_date = today + timedelta(days=days_to_thu)
    months = ["JAN","FEB","MAR","APR","MAY","JUN",
              "JUL","AUG","SEP","OCT","NOV","DEC"]
    month_str = months[expiry_date.month - 1]
    return f"{expiry_date.day:02d}{month_str}{expiry_date.year}"
