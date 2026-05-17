"""
core/groww_execution_engine.py
==============================
Groww Semi-Auto Order Filler for TradingBot.

Safety model:
    DRY_RUN = True  (default) — all fill/click/approval actions SIMULATED.
                                No DOM mutations. No Telegram calls. Safe always.
    DRY_RUN = False           — real fills + Telegram approval gate + ENTER fallback
                                before final Buy/Sell click.

Approval workflow (DRY_RUN=False):
    1. Connect Chrome CDP port 9333
    2. Detect Groww F&O order modal
    3. Extract instrument / strike / CE-PE / premium
    4. Fill qty + order type + optional limit price
    5. Send Telegram approval request (instrument, confidence, SL, targets, risk)
    6. Poll Telegram for APPROVE / REJECT reply (timeout = configurable)
    7. On APPROVE → terminal ENTER fallback prompt → click confirm button
    8. On REJECT / EXPIRED → abort, send Telegram outcome message

Never call run() with DRY_RUN=False from within the automated signal loop.
Order execution must always be human-initiated.
"""

import sys
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout

from config.config import (
    STOP_LOSS_POINTS,
    TARGET_1_POINTS,
    TARGET_2_POINTS,
    TARGET_3_POINTS,
    TELEGRAM_APPROVAL_TIMEOUT_MINUTES,
)
from core.telegram_approval import (
    request_approval,
    OrderApprovalDetails,
    ApprovalOutcome,
)

from core.paper_trading_guard import (
    enforce_paper_mode,
    block_if_paper,
    paper_tag,
)

# Enforce paper trading mode at import time — raises if PAPER_TRADING_MODE is ever disabled
enforce_paper_mode()

# =========================
# SAFETY FLAG
# =========================
# True  → simulate only, no DOM writes, no Telegram calls
# False → real fills + Telegram approval + ENTER + final click

DRY_RUN: bool = True

# =========================
# CONFIGURATION
# =========================

GROWW_DEBUG_URL   = "http://127.0.0.1:9333"
GROWW_DOMAIN      = "groww.in"

SCREENSHOT_DIR = Path(__file__).parent.parent / "screenshots" / "groww_orders"
LOG_DIR        = Path(__file__).parent.parent / "trade_logs"
LOG_FILE       = LOG_DIR / "groww_execution_log.txt"

DEFAULT_QUANTITY:    int   = 1
DEFAULT_ORDER_TYPE:  str   = "MARKET"
DEFAULT_LIMIT_PRICE: float = 0.0

ELEMENT_TIMEOUT_MS: int = 5_000
FILL_TIMEOUT_MS:    int = 3_000

# =========================
# GROWW SELECTORS
# =========================

MODAL_SELECTORS = [
    '[class*="orderModal"]',
    '[class*="BuySellModal"]',
    '[class*="placeOrder"]',
    '[class*="order-modal"]',
    '[data-testid="order-modal"]',
]

INSTRUMENT_SELECTORS = [
    '[class*="instrumentName"]',
    '[class*="symbolName"]',
    '[class*="stockName"]',
    '[class*="scriptName"]',
    'h2[class*="Modal"]',
    '[data-testid="instrument-name"]',
]

PREMIUM_SELECTORS = [
    '[class*="ltp"]',
    '[class*="lastTradePrice"]',
    '[class*="currentPrice"]',
    '[class*="premiumValue"]',
    '[data-testid="ltp"]',
]

QTY_SELECTORS = [
    'input[placeholder*="Qty"]',
    'input[placeholder*="qty"]',
    'input[placeholder*="Quantity"]',
    'input[aria-label*="Quantity"]',
    'input[data-testid="quantity-input"]',
    'input[class*="quantityInput"]',
]

ORDER_TYPE_SELECTORS = {
    "MARKET": [
        'button[data-testid="order-type-market"]',
        '[class*="marketOrder"]',
        'button:has-text("Market")',
    ],
    "LIMIT": [
        'button[data-testid="order-type-limit"]',
        '[class*="limitOrder"]',
        'button:has-text("Limit")',
    ],
}

LIMIT_PRICE_SELECTORS = [
    'input[placeholder*="Price"]',
    'input[placeholder*="price"]',
    'input[aria-label*="Price"]',
    'input[data-testid="limit-price-input"]',
    'input[class*="priceInput"]',
]

CONFIRM_BUY_SELECTORS = [
    'button[data-testid="buy-confirm"]',
    'button[class*="buyButton"]',
    'button:has-text("Buy")',
    '[class*="confirmBuy"]',
]

CONFIRM_SELL_SELECTORS = [
    'button[data-testid="sell-confirm"]',
    'button[class*="sellButton"]',
    'button:has-text("Sell")',
    '[class*="confirmSell"]',
]

# =========================
# LOGGING SETUP
# =========================

LOG_DIR.mkdir(parents=True, exist_ok=True)
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(LOG_FILE), mode="a", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

SEP  = "=" * 52
SEP2 = "-" * 52


# =========================
# ORDER PARAMS
# =========================

class OrderParams:
    """
    Container for order parameters. Includes confidence data for
    the Telegram approval message.

    Attributes:
        quantity         (int)   : Lots
        order_type       (str)   : "MARKET" or "LIMIT"
        limit_price      (float) : Used only when order_type="LIMIT"
        side             (str)   : "BUY" or "SELL"
        confidence_score (int)   : 0-100 from MultiTimeframeAnalyzer
        confidence_level (str)   : "HIGH" / "MEDIUM" / "LOW"
        max_loss_inr     (float) : From RiskEngine.calculate_position_size()
    """
    def __init__(
        self,
        quantity:         int   = DEFAULT_QUANTITY,
        order_type:       str   = DEFAULT_ORDER_TYPE,
        limit_price:      float = DEFAULT_LIMIT_PRICE,
        side:             str   = "BUY",
        confidence_score: int   = 0,
        confidence_level: str   = "UNKNOWN",
        max_loss_inr:     float = 0.0,
    ):
        self.quantity         = quantity
        self.order_type       = order_type.upper()
        self.limit_price      = limit_price
        self.side             = side.upper()
        self.confidence_score = confidence_score
        self.confidence_level = confidence_level
        self.max_loss_inr     = max_loss_inr

    def __repr__(self) -> str:
        return (
            f"OrderParams(qty={self.quantity}, type={self.order_type}, "
            f"side={self.side}, confidence={self.confidence_score}/{self.confidence_level})"
        )


# =========================
# EXECUTION RESULT
# =========================

class ExecutionResult:
    def __init__(self):
        self.success         = False
        self.dry_run         = DRY_RUN
        self.order_placed    = False
        self.approval_outcome: Optional[str] = None
        self.instrument      = None
        self.strike          = None
        self.option_type     = None
        self.premium         = None
        self.screenshots     = []
        self.error           = None

    def __repr__(self) -> str:
        return (
            f"ExecutionResult(success={self.success}, dry_run={self.dry_run}, "
            f"order_placed={self.order_placed}, approval={self.approval_outcome}, "
            f"instrument={self.instrument}, strike={self.strike}, "
            f"option_type={self.option_type}, premium={self.premium})"
        )


# =========================
# INTERNAL HELPERS
# =========================

def _ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _screenshot(page: Page, label: str, result: ExecutionResult) -> Optional[Path]:
    path = SCREENSHOT_DIR / f"groww_{label}_{_ts()}.png"
    try:
        page.screenshot(path=str(path), full_page=False)
        result.screenshots.append(path)
        logger.info(f"[SCREENSHOT] Saved: {path.name}")
        return path
    except Exception as e:
        logger.warning(f"[SCREENSHOT] Failed ({label}): {e}")
        return None


def _first_match(page: Page, selectors: list, timeout: int = ELEMENT_TIMEOUT_MS):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=timeout)
            return loc
        except Exception:
            continue
    return None


def _read_text(page: Page, selectors: list) -> str:
    loc = _first_match(page, selectors, timeout=3_000)
    if loc is None:
        return ""
    try:
        return loc.inner_text().strip()
    except Exception:
        return ""


def _parse_instrument(raw: str):
    import re
    if not raw:
        return raw, None, None
    pattern = re.search(r'([A-Z]+(?:\d+)?)\D*?(\d{4,6})\s*(CE|PE)', raw.upper())
    if pattern:
        return pattern.group(1), pattern.group(2), pattern.group(3)
    opt = "CE" if "CE" in raw.upper() else ("PE" if "PE" in raw.upper() else None)
    return raw.strip(), None, opt


def _parse_premium(raw: str) -> Optional[float]:
    import re
    raw = raw.replace(",", "")
    match = re.search(r'[\d]+\.?\d*', raw)
    if match:
        try:
            return float(match.group())
        except ValueError:
            pass
    return None


def _safe_fill(page: Page, selectors: list, value: str, label: str, dry_run: bool) -> bool:
    if dry_run:
        logger.info(f"[DRY RUN] Would fill '{label}' with: {value}")
        return True
    loc = _first_match(page, selectors, timeout=FILL_TIMEOUT_MS)
    if loc is None:
        logger.warning(f"[FILL] Could not locate field: {label}")
        return False
    try:
        loc.triple_click()
        loc.type(value, delay=50)
        logger.info(f"[FILL] '{label}' filled with: {value}")
        return True
    except Exception as e:
        logger.error(f"[FILL] Failed '{label}': {e}")
        return False


def _safe_click(page: Page, selectors: list, label: str, dry_run: bool) -> bool:
    if dry_run:
        logger.info(f"[DRY RUN] Would click: {label}")
        return True
    loc = _first_match(page, selectors, timeout=FILL_TIMEOUT_MS)
    if loc is None:
        logger.warning(f"[CLICK] Could not locate: {label}")
        return False
    try:
        loc.click()
        logger.info(f"[CLICK] Clicked: {label}")
        return True
    except Exception as e:
        logger.error(f"[CLICK] Failed '{label}': {e}")
        return False


# =========================
# CHROME / MODAL DETECTION
# =========================

def detect_groww_page(browser) -> Optional[Page]:
    for ctx in browser.contexts:
        for page in ctx.pages:
            try:
                if GROWW_DOMAIN in page.url:
                    return page
            except Exception:
                continue
    return None


def detect_order_modal(page: Page) -> bool:
    for sel in MODAL_SELECTORS:
        try:
            if page.locator(sel).count() > 0:
                logger.info(f"[MODAL] Detected via: {sel}")
                return True
        except Exception:
            continue
    return False


# =========================
# EXTRACTION
# =========================

def extract_modal_data(page: Page) -> dict:
    raw_instrument = _read_text(page, INSTRUMENT_SELECTORS)
    raw_premium    = _read_text(page, PREMIUM_SELECTORS)
    instrument, strike, option_type = _parse_instrument(raw_instrument)
    premium = _parse_premium(raw_premium)

    logger.info(f"[EXTRACT] {instrument} {strike} {option_type} | premium={premium}")
    return {
        "raw_instrument": raw_instrument,
        "instrument":     instrument,
        "strike":         strike,
        "option_type":    option_type,
        "premium":        premium,
        "raw_premium":    raw_premium,
    }


# =========================
# FIELD FILL
# =========================

def fill_order_fields(page: Page, params: OrderParams, dry_run: bool) -> bool:
    logger.info(SEP2)
    logger.info(f"[FILL] Filling order fields | dry_run={dry_run}")
    all_ok = True

    type_selectors = ORDER_TYPE_SELECTORS.get(params.order_type, [])
    if not _safe_click(page, type_selectors, f"Order Type: {params.order_type}", dry_run):
        all_ok = False

    if not _safe_fill(page, QTY_SELECTORS, str(params.quantity), "Quantity", dry_run):
        all_ok = False

    if params.order_type == "LIMIT" and params.limit_price > 0:
        if not _safe_fill(page, LIMIT_PRICE_SELECTORS,
                          f"{params.limit_price:.2f}", "Limit Price", dry_run):
            all_ok = False
    elif params.order_type == "LIMIT":
        logger.warning("[FILL] LIMIT order but limit_price=0 — skipping price fill")

    return all_ok


# =========================
# TERMINAL ENTER FALLBACK
# =========================

def _await_terminal_enter(instrument: str, strike: str, option_type: str) -> bool:
    """
    Final ENTER confirmation in the terminal — last human gate before click.
    Returns True if ENTER pressed, False if Ctrl+C / EOF.
    This runs AFTER Telegram APPROVE is received.
    """
    print()
    print(SEP2)
    print(f"  Telegram APPROVED. Ready to place: {instrument} {strike} {option_type}")
    print("  Press ENTER to confirm final click | Ctrl+C to abort")
    print(SEP2)
    try:
        input()
        logger.info("[CONFIRM] Terminal ENTER received — proceeding to place order")
        return True
    except (KeyboardInterrupt, EOFError):
        logger.warning("[CONFIRM] Terminal abort (Ctrl+C) — order cancelled after approval")
        return False


# =========================
# ORDER PLACEMENT
# =========================

@block_if_paper
def place_order(page: Page, params: OrderParams, dry_run: bool) -> bool:
    """
    Click the Buy/Sell confirm button.
    Only reachable when dry_run=False AND Telegram APPROVED AND ENTER pressed.
    Has an internal dry_run guard as a safety belt.
    """
    if dry_run:
        logger.warning("[SAFETY] place_order() called with dry_run=True — BLOCKED")
        return False

    selectors = CONFIRM_BUY_SELECTORS if params.side == "BUY" else CONFIRM_SELL_SELECTORS
    label     = f"{params.side} Confirm Button"
    logger.info(f"[ORDER] Clicking {label}...")
    return _safe_click(page, selectors, label, dry_run=False)


# =========================
# MAIN RUN FUNCTION
# =========================

def run(params: Optional[OrderParams] = None) -> ExecutionResult:
    """
    Entry point for the Groww execution engine.

    Args:
        params: OrderParams with order configuration + confidence metadata.
                Uses defaults if None.

    Returns:
        ExecutionResult with full audit trail.
    """
    if params is None:
        params = OrderParams()

    result         = ExecutionResult()
    result.dry_run = DRY_RUN

    logger.info(SEP)
    logger.info("GROWW EXECUTION ENGINE")
    logger.info(f"DRY_RUN    : {DRY_RUN}")
    logger.info(f"Params     : {params}")
    logger.info(f"Debug URL  : {GROWW_DEBUG_URL}")
    logger.info(f"Approval   : Telegram | timeout={TELEGRAM_APPROVAL_TIMEOUT_MINUTES}min")
    logger.info(SEP)

    if DRY_RUN:
        logger.info("[SAFETY] DRY_RUN=True — all actions simulated, no real execution")

    with sync_playwright() as p:

        # =========================
        # STEP 1: CONNECT
        # =========================

        try:
            browser = p.chromium.connect_over_cdp(GROWW_DEBUG_URL)
            logger.info(f"[CHROME] Connected | contexts={len(browser.contexts)}")
        except Exception as e:
            result.error = f"Cannot connect to Chrome on {GROWW_DEBUG_URL}: {e}"
            logger.critical(f"[CHROME] {result.error}")
            return result

        # =========================
        # STEP 2: FIND GROWW TAB
        # =========================

        page = detect_groww_page(browser)
        if page is None:
            result.error = f"No Groww tab found (domain: {GROWW_DOMAIN})"
            logger.error(f"[GROWW] {result.error}")
            return result

        logger.info(f"[GROWW] Tab: {page.url}")

        # =========================
        # STEP 3: DETECT MODAL
        # =========================

        if not detect_order_modal(page):
            result.error = "No F&O order modal detected"
            logger.error(f"[MODAL] {result.error}")
            _screenshot(page, "no_modal", result)
            return result

        # =========================
        # STEP 4: EXTRACT INSTRUMENT DATA
        # =========================

        _screenshot(page, "pre_fill", result)
        modal_data = extract_modal_data(page)

        result.instrument  = modal_data["instrument"]
        result.strike      = modal_data["strike"]
        result.option_type = modal_data["option_type"]
        result.premium     = modal_data["premium"]

        logger.info(SEP2)
        logger.info("[SUMMARY] Order details:")
        logger.info(f"  Instrument  : {result.instrument} {result.strike} {result.option_type}")
        logger.info(f"  Premium     : {result.premium}")
        logger.info(f"  Side        : {params.side}")
        logger.info(f"  Qty         : {params.quantity} lot(s)")
        logger.info(f"  Order Type  : {params.order_type}")
        logger.info(f"  Confidence  : {params.confidence_score}/100 [{params.confidence_level}]")
        logger.info(f"  Max Loss    : Rs.{params.max_loss_inr:.0f}")
        logger.info(SEP2)

        # =========================
        # STEP 5: FILL FIELDS
        # =========================

        fill_order_fields(page, params, dry_run=DRY_RUN)
        _screenshot(page, "post_fill", result)

        # =========================
        # STEP 6: DRY RUN — SIMULATE APPROVAL AND EXIT
        # =========================

        if DRY_RUN:
            # Simulate approval gate
            approval = request_approval(
                details=OrderApprovalDetails(
                    instrument=       result.instrument or "UNKNOWN",
                    strike=           result.strike or "?",
                    option_type=      result.option_type or "?",
                    quantity=         params.quantity,
                    side=             params.side,
                    premium=          result.premium,
                    confidence_score= params.confidence_score,
                    confidence_level= params.confidence_level,
                    stop_loss_pts=    STOP_LOSS_POINTS,
                    target1_pts=      TARGET_1_POINTS,
                    target2_pts=      TARGET_2_POINTS,
                    target3_pts=      TARGET_3_POINTS,
                    max_loss_inr=     params.max_loss_inr,
                    order_type=       params.order_type,
                    limit_price=      params.limit_price,
                ),
                dry_run=True,
            )
            result.approval_outcome = approval.outcome.value
            _screenshot(page, "dry_run_final", result)
            logger.info("[DRY RUN] Complete. No real actions taken.")
            logger.info(f"[DRY RUN] Set DRY_RUN=False in {__file__} for live execution.")
            result.success = True
            return result

        # =========================
        # STEP 7: TELEGRAM APPROVAL GATE
        # Sends approval request, polls for APPROVE/REJECT/timeout.
        # =========================

        approval = request_approval(
            details=OrderApprovalDetails(
                instrument=       result.instrument or "UNKNOWN",
                strike=           result.strike or "?",
                option_type=      result.option_type or "?",
                quantity=         params.quantity,
                side=             params.side,
                premium=          result.premium,
                confidence_score= params.confidence_score,
                confidence_level= params.confidence_level,
                stop_loss_pts=    STOP_LOSS_POINTS,
                target1_pts=      TARGET_1_POINTS,
                target2_pts=      TARGET_2_POINTS,
                target3_pts=      TARGET_3_POINTS,
                max_loss_inr=     params.max_loss_inr,
                order_type=       params.order_type,
                limit_price=      params.limit_price,
            ),
            dry_run=False,
        )

        result.approval_outcome = approval.outcome.value
        logger.info(f"[APPROVAL] Outcome: {approval.outcome.value} | elapsed={approval.elapsed_secs:.1f}s")

        # --- REJECTED ---
        if approval.outcome == ApprovalOutcome.REJECTED:
            logger.warning("[ORDER] Rejected via Telegram — no order placed")
            _screenshot(page, "rejected", result)
            result.success = True
            return result

        # --- EXPIRED ---
        if approval.outcome == ApprovalOutcome.EXPIRED:
            logger.warning("[ORDER] Approval timed out — no order placed")
            _screenshot(page, "expired", result)
            result.success = True
            return result

        # --- ERROR ---
        if approval.outcome == ApprovalOutcome.ERROR:
            result.error = approval.error or "Telegram approval error"
            logger.error(f"[ORDER] {result.error} — aborting")
            _screenshot(page, "approval_error", result)
            return result

        # =========================
        # STEP 8: TERMINAL ENTER FALLBACK
        # Reached only on APPROVE. Secondary human confirmation before click.
        # =========================

        terminal_ok = _await_terminal_enter(
            instrument=  result.instrument or "?",
            strike=      result.strike or "?",
            option_type= result.option_type or "?",
        )

        if not terminal_ok:
            logger.warning("[ORDER] Terminal abort after Telegram APPROVE — no order placed")
            _screenshot(page, "terminal_aborted", result)
            result.success = True
            return result

        # =========================
        # STEP 9: PLACE ORDER
        # Reached only when:
        #   - DRY_RUN=False AND
        #   - Telegram APPROVED AND
        #   - Terminal ENTER pressed
        # =========================

        placed = place_order(page, params, dry_run=False)

        if placed:
            logger.info(f"[ORDER] {params.side} order submitted successfully")
            result.order_placed = True
        else:
            result.error = "Confirm button click failed — verify order status on Groww"
            logger.error(f"[ORDER] {result.error}")

        _screenshot(page, "post_order", result)
        result.success = True

    logger.info(SEP)
    logger.info(f"[DONE] {result}")
    logger.info(SEP)

    return result


# =========================
# ENTRY POINT
# =========================

if __name__ == "__main__":
    params = OrderParams(
        quantity=         1,
        order_type=       "MARKET",
        limit_price=      0.0,
        side=             "BUY",
        confidence_score= 0,
        confidence_level= "UNKNOWN",
        max_loss_inr=     0.0,
    )
    result = run(params)
    sys.exit(0 if result.success else 1)
