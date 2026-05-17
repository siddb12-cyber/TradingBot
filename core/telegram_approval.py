"""
core/telegram_approval.py
=========================
Telegram-based order approval gate for TradingBot.

Responsibilities:
    1. Send a detailed order approval request to the configured Telegram chat
    2. Poll Telegram getUpdates for an APPROVE or REJECT reply from CHAT_ID
    3. Auto-cancel after TELEGRAM_APPROVAL_TIMEOUT_MINUTES if no reply
    4. Send a confirmation message back for every outcome (approved/rejected/expired)
    5. Return ApprovalResult with outcome and audit metadata

DRY_RUN behaviour (imported from groww_execution_engine context):
    Callers pass dry_run=True → gate is simulated, no Telegram calls made.
    This module accepts dry_run as a parameter — it does not import DRY_RUN itself,
    keeping it a pure utility with no global state dependency.

Polling strategy:
    - Uses long-polling getUpdates with timeout=2s per request
    - Tracks update_id offset to never re-process old messages
    - Only messages from CHAT_ID are accepted as valid approvals
    - Message text is stripped and uppercased before matching

Usage:
    from core.telegram_approval import request_approval, ApprovalOutcome, OrderApprovalDetails

    details = OrderApprovalDetails(
        instrument="NIFTY", strike="23800", option_type="CE",
        quantity=2, side="BUY", premium=145.50,
        confidence_score=82, confidence_level="HIGH",
        stop_loss_pts=10, target1_pts=15, target2_pts=25, target3_pts=40,
        max_loss_inr=750.0, order_type="MARKET", limit_price=0.0,
    )
    result = request_approval(details, dry_run=False)

    if result.outcome == ApprovalOutcome.APPROVED:
        # proceed to place_order()
    elif result.outcome == ApprovalOutcome.REJECTED:
        # abort
    elif result.outcome == ApprovalOutcome.EXPIRED:
        # timed out — abort
"""

import time
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

import requests

from config.config import (
    BOT_TOKEN,
    CHAT_ID,
    TELEGRAM_APPROVAL_TIMEOUT_MINUTES,
    TELEGRAM_POLL_INTERVAL_SECONDS,
    STOP_LOSS_POINTS,
    TARGET_1_POINTS,
    TARGET_2_POINTS,
    TARGET_3_POINTS,
)

# =========================
# LOGGING
# =========================

logger = logging.getLogger(__name__)

# =========================
# TELEGRAM API URLS
# =========================

_TG_BASE       = f"https://api.telegram.org/bot{BOT_TOKEN}"
_SEND_URL      = f"{_TG_BASE}/sendMessage"
_UPDATES_URL   = f"{_TG_BASE}/getUpdates"

# Keyword strings accepted as approval / rejection
_APPROVE_KEYWORDS = {"APPROVE", "APPROVED", "YES", "Y"}
_REJECT_KEYWORDS  = {"REJECT", "REJECTED", "NO", "N", "CANCEL", "ABORT"}

SEP = "--" * 16


# =========================
# APPROVAL OUTCOME ENUM
# =========================

class ApprovalOutcome(str, Enum):
    APPROVED = "APPROVED"   # User replied APPROVE within timeout
    REJECTED = "REJECTED"   # User replied REJECT within timeout
    EXPIRED  = "EXPIRED"    # No reply within TELEGRAM_APPROVAL_TIMEOUT_MINUTES
    DRY_RUN  = "DRY_RUN"    # Simulated — no real Telegram call made
    ERROR    = "ERROR"      # Telegram API or network error


# =========================
# ORDER APPROVAL DETAILS
# =========================

@dataclass
class OrderApprovalDetails:
    """
    All data included in the Telegram approval request message.
    Passed by the execution engine before order placement.
    """
    instrument:       str
    strike:           str
    option_type:      str           # "CE" or "PE"
    quantity:         int
    side:             str           # "BUY" or "SELL"
    premium:          Optional[float]
    confidence_score: int
    confidence_level: str
    stop_loss_pts:    int   = STOP_LOSS_POINTS
    target1_pts:      int   = TARGET_1_POINTS
    target2_pts:      int   = TARGET_2_POINTS
    target3_pts:      int   = TARGET_3_POINTS
    max_loss_inr:     float = 0.0
    order_type:       str   = "MARKET"
    limit_price:      float = 0.0


# =========================
# APPROVAL RESULT
# =========================

@dataclass
class ApprovalResult:
    """
    Returned by request_approval(). Contains outcome + full audit trail.
    """
    outcome:      ApprovalOutcome = ApprovalOutcome.ERROR
    dry_run:      bool            = False
    replied_at:   Optional[str]   = None    # ISO timestamp of reply
    elapsed_secs: float           = 0.0
    reply_text:   Optional[str]   = None    # Raw text received from Telegram
    error:        Optional[str]   = None

    @property
    def approved(self) -> bool:
        return self.outcome == ApprovalOutcome.APPROVED


# =========================
# INTERNAL TELEGRAM HELPERS
# =========================

def _send(text: str) -> bool:
    """Send a message to CHAT_ID. Returns True if HTTP 200."""
    try:
        r = requests.post(
            _SEND_URL,
            data={"chat_id": CHAT_ID, "text": text},
            timeout=10,
        )
        if r.status_code == 200:
            logger.debug("[APPROVAL] Telegram message sent")
            return True
        logger.warning(f"[APPROVAL] Send failed: {r.status_code} {r.text[:100]}")
        return False
    except requests.RequestException as e:
        logger.error(f"[APPROVAL] Send error: {e}")
        return False


def _get_updates(offset: int) -> Optional[list]:
    """
    Fetch new updates from Telegram with short long-poll timeout.
    Returns list of update dicts, or None on error.
    offset: next update_id to fetch (skips already-seen messages).
    """
    try:
        r = requests.get(
            _UPDATES_URL,
            params={"offset": offset, "timeout": 2, "limit": 10},
            timeout=8,
        )
        if r.status_code != 200:
            logger.warning(f"[APPROVAL] getUpdates failed: {r.status_code}")
            return None
        data = r.json()
        if not data.get("ok"):
            return None
        return data.get("result", [])
    except requests.RequestException as e:
        logger.debug(f"[APPROVAL] getUpdates error: {e}")
        return None


def _latest_update_id() -> int:
    """
    Fetch the current highest update_id so we only watch for NEW messages.
    Returns 0 if none found (fresh start).
    """
    updates = _get_updates(offset=-1)   # -1 = fetch the single most recent update
    if updates:
        return updates[-1]["update_id"]
    return 0


def _check_for_reply(updates: list) -> Optional[str]:
    """
    Scan updates for a message from CHAT_ID.
    Returns the raw message text if found from CHAT_ID, else None.
    Only processes 'message' type updates (not edited_message, callback_query, etc.)
    """
    chat_id_str = str(CHAT_ID)

    for update in updates:
        msg = update.get("message")
        if not msg:
            continue

        sender_chat = str(msg.get("chat", {}).get("id", ""))
        if sender_chat != chat_id_str:
            logger.debug(
                f"[APPROVAL] Ignoring message from unknown chat_id: {sender_chat}"
            )
            continue

        text = msg.get("text", "").strip()
        if text:
            return text

    return None


# =========================
# MESSAGE BUILDERS
# =========================

def _build_approval_request(d: OrderApprovalDetails, timeout_min: int) -> str:
    """Build the approval request message sent to Telegram."""
    premium_str = f"{d.premium:.2f}" if d.premium else "N/A"
    limit_str   = (
        f"\nLimit Price : {d.limit_price:.2f}"
        if d.order_type == "LIMIT" and d.limit_price > 0
        else ""
    )
    return (
        f"ORDER APPROVAL REQUEST\n{SEP}\n"
        f"Instrument  : {d.instrument} {d.strike} {d.option_type}\n"
        f"Side        : {d.side}\n"
        f"Premium     : {premium_str}\n"
        f"Qty (Lots)  : {d.quantity}\n"
        f"Order Type  : {d.order_type}{limit_str}\n"
        f"{SEP}\n"
        f"Stop Loss   : {d.stop_loss_pts} pts\n"
        f"Target 1    : {d.target1_pts} pts\n"
        f"Target 2    : {d.target2_pts} pts\n"
        f"Target 3    : {d.target3_pts} pts\n"
        f"Max Loss    : Rs.{d.max_loss_inr:.0f}\n"
        f"{SEP}\n"
        f"Confidence  : {d.confidence_score}/100  [{d.confidence_level}]\n"
        f"{SEP}\n"
        f"Reply APPROVE to place order\n"
        f"Reply REJECT to cancel\n"
        f"Auto-cancels in {timeout_min} min\n"
        f"[Paper Trading Mode]"
    )


def _build_outcome_msg(outcome: ApprovalOutcome, details: OrderApprovalDetails,
                        elapsed: float) -> str:
    """Build the outcome notification message sent back to Telegram."""
    instrument = f"{details.instrument} {details.strike} {details.option_type}"

    if outcome == ApprovalOutcome.APPROVED:
        return (
            f"ORDER APPROVED\n{SEP}\n"
            f"{instrument} | {details.side} {details.quantity} lot(s)\n"
            f"Placing order now...\n"
            f"[Elapsed: {elapsed:.0f}s]"
        )
    elif outcome == ApprovalOutcome.REJECTED:
        return (
            f"ORDER REJECTED\n{SEP}\n"
            f"{instrument}\n"
            f"Order cancelled by operator.\n"
            f"[Elapsed: {elapsed:.0f}s]"
        )
    else:  # EXPIRED
        return (
            f"ORDER EXPIRED\n{SEP}\n"
            f"{instrument}\n"
            f"No reply received within {TELEGRAM_APPROVAL_TIMEOUT_MINUTES} min.\n"
            f"Order auto-cancelled.\n"
            f"[Paper Trading Mode]"
        )


# =========================
# PRIMARY PUBLIC FUNCTION
# =========================

def request_approval(
    details:  OrderApprovalDetails,
    dry_run:  bool = True,
    timeout_minutes: int = TELEGRAM_APPROVAL_TIMEOUT_MINUTES,
    poll_interval:   int = TELEGRAM_POLL_INTERVAL_SECONDS,
) -> ApprovalResult:
    """
    Send a Telegram approval request and poll for APPROVE/REJECT reply.

    Args:
        details:         Full order details for the approval message
        dry_run:         If True, simulate approval without any Telegram calls
        timeout_minutes: Minutes before auto-cancellation (default from config)
        poll_interval:   Seconds between getUpdates polls (default from config)

    Returns:
        ApprovalResult with outcome, timing, and audit fields

    Flow:
        1. [dry_run=True]  → log simulation, return DRY_RUN outcome immediately
        2. Fetch current update offset to ignore old messages
        3. Send approval request message to CHAT_ID
        4. Poll getUpdates every poll_interval seconds
        5. On APPROVE keyword → send approved msg → return APPROVED
        6. On REJECT keyword → send rejected msg → return REJECTED
        7. On timeout → send expired msg → return EXPIRED
    """
    result = ApprovalResult(dry_run=dry_run)
    start  = datetime.now()

    # =========================
    # DRY RUN — SIMULATE ONLY
    # =========================

    if dry_run:
        logger.info("[APPROVAL] DRY RUN — simulating Telegram approval gate")
        logger.info(
            f"[APPROVAL] Would request approval for: "
            f"{details.instrument} {details.strike} {details.option_type} "
            f"{details.side} {details.quantity}lot(s) | "
            f"confidence={details.confidence_score}/100"
        )
        result.outcome      = ApprovalOutcome.DRY_RUN
        result.elapsed_secs = 0.0
        return result

    # =========================
    # VALIDATE CONFIG
    # =========================

    if not BOT_TOKEN or not CHAT_ID:
        result.error   = "BOT_TOKEN or CHAT_ID not configured in .env"
        result.outcome = ApprovalOutcome.ERROR
        logger.error(f"[APPROVAL] {result.error}")
        return result

    # =========================
    # STEP 1: ANCHOR OFFSET
    # Fetch the current highest update_id so we only watch NEW replies.
    # =========================

    logger.info("[APPROVAL] Fetching current update offset...")
    offset = _latest_update_id() + 1
    logger.info(f"[APPROVAL] Watching for updates from offset={offset}")

    # =========================
    # STEP 2: SEND APPROVAL REQUEST
    # =========================

    msg = _build_approval_request(details, timeout_minutes)
    logger.info(
        f"[APPROVAL] Sending approval request to CHAT_ID={CHAT_ID} | "
        f"timeout={timeout_minutes}min"
    )

    sent = _send(msg)
    if not sent:
        result.error   = "Failed to send approval request to Telegram"
        result.outcome = ApprovalOutcome.ERROR
        logger.error(f"[APPROVAL] {result.error}")
        return result

    logger.info(f"[APPROVAL] Request sent. Waiting for APPROVE / REJECT...")

    # =========================
    # STEP 3: POLL FOR REPLY
    # =========================

    deadline = start + timedelta(minutes=timeout_minutes)
    elapsed  = 0.0

    while datetime.now() < deadline:

        updates = _get_updates(offset)

        if updates is not None:
            # Advance offset past all fetched updates to avoid re-processing
            for u in updates:
                offset = max(offset, u["update_id"] + 1)

            reply_text = _check_for_reply(updates)

            if reply_text is not None:
                keyword = reply_text.strip().upper()
                elapsed = (datetime.now() - start).total_seconds()
                result.reply_text   = reply_text
                result.replied_at   = datetime.now().isoformat()
                result.elapsed_secs = elapsed

                if keyword in _APPROVE_KEYWORDS:
                    result.outcome = ApprovalOutcome.APPROVED
                    logger.info(
                        f"[APPROVAL] APPROVED by operator | "
                        f"reply='{reply_text}' | elapsed={elapsed:.1f}s"
                    )
                    _send(_build_outcome_msg(ApprovalOutcome.APPROVED, details, elapsed))
                    return result

                elif keyword in _REJECT_KEYWORDS:
                    result.outcome = ApprovalOutcome.REJECTED
                    logger.info(
                        f"[APPROVAL] REJECTED by operator | "
                        f"reply='{reply_text}' | elapsed={elapsed:.1f}s"
                    )
                    _send(_build_outcome_msg(ApprovalOutcome.REJECTED, details, elapsed))
                    return result

                else:
                    # Unknown keyword — log and keep waiting
                    logger.info(
                        f"[APPROVAL] Unrecognized reply: '{reply_text}' | "
                        f"Expected: APPROVE or REJECT — still waiting..."
                    )

        time.sleep(poll_interval)
        elapsed = (datetime.now() - start).total_seconds()
        remaining = (deadline - datetime.now()).total_seconds()
        logger.info(
            f"[APPROVAL] Polling... elapsed={elapsed:.0f}s "
            f"remaining={max(remaining, 0):.0f}s"
        )

    # =========================
    # STEP 4: TIMEOUT — AUTO-CANCEL
    # =========================

    elapsed = (datetime.now() - start).total_seconds()
    result.outcome      = ApprovalOutcome.EXPIRED
    result.elapsed_secs = elapsed

    logger.warning(
        f"[APPROVAL] Timed out after {elapsed:.0f}s "
        f"({timeout_minutes}min) — order auto-cancelled"
    )
    _send(_build_outcome_msg(ApprovalOutcome.EXPIRED, details, elapsed))

    return result
