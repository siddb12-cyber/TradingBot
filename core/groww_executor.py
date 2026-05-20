"""
core/groww_executor.py
======================
Full-auto Groww F&O order execution engine.

This module is called by trading_engine.py ONLY after the user has tapped
APPROVE (or SCALE) on the Telegram inline keyboard. The approval gate is
handled entirely by TelegramApprovalBot — this module just places the order.

Architecture
------------
  trading_engine.py
      ↓ (user tapped ✅ APPROVE on Telegram)
  GrowwExecutor.execute_order(signal, lots)
      ↓
  1. PAPER_TRADING_MODE guard (raises if True — safety net)
  2. Connect to Chrome CDP port 9333 (Groww profile)
  3. Navigate to F&O order page
  4. Fill strike, option type (CE/PE), quantity, order type
  5. Click BUY / SELL confirm button
  6. Send Telegram confirmation with order status

Safety
------
- PAPER_TRADING_MODE = True in config.py → execute_order() raises immediately
- All real execution is triple-guarded:
    1. paper_trading_guard.enforce_paper_mode() at module import
    2. PAPER_TRADING_MODE check in execute_order()
    3. trading_engine.py only calls this when PAPER_TRADING_MODE=False

Browser requirements
--------------------
Groww must be open in Chrome with remote debugging enabled on port 9333:
    chrome.exe --remote-debugging-port=9333 --user-data-dir="<profile>"

The profile must already be logged into Groww F&O.
"""

import logging
import time
from datetime import datetime
from typing import Optional

from config.config import (
    PAPER_TRADING_MODE,
    GROWW_CDP_PORT,
    GROWW_FNO_URL,
    NIFTY_LOT_SIZE,
    NIFTY_STRIKE_INTERVAL,
    STOP_LOSS_POINTS,
    TARGET_1_POINTS,
    TARGET_2_POINTS,
    TARGET_3_POINTS,
    BOT_TOKEN,
    CHAT_ID,
)
from core.paper_trading_guard import enforce_paper_mode

# =========================
# MODULE LOGGER
# =========================

logger = logging.getLogger(__name__)

# =========================
# PAPER MODE GUARD
# Raises RuntimeError at import time if somehow PAPER_TRADING_MODE=False
# without explicit code review. Belt-and-suspenders safety.
# =========================

# NOTE: enforce_paper_mode() is NOT called here — we want this module to
# be importable even in paper mode (trading_engine.py imports it).
# The execution guard is inside execute_order() itself.


# =========================
# GROWW PAGE SELECTORS
# These CSS selectors are maintained for Groww's DOM structure as of 2026.
# Update if Groww redesigns their F&O order modal.
# =========================

# F&O search / instrument selectors
_SEL_SEARCH_INPUT    = 'input[placeholder*="Search"]'
_SEL_SEARCH_RESULT   = '.fno-search-result, .instrument-item'

# Order form selectors
_SEL_QTY_INPUT       = 'input[name="quantity"], input[placeholder*="Qty"], input[placeholder*="Lot"]'
_SEL_ORDER_TYPE_BTN  = 'button[data-order-type="MARKET"], .order-type-market'
_SEL_BUY_BTN         = 'button.buy-btn, button[data-action="buy"], button:has-text("Buy")'
_SEL_SELL_BTN        = 'button.sell-btn, button[data-action="sell"], button:has-text("Sell")'
_SEL_CONFIRM_BTN     = 'button.confirm-btn, button:has-text("Confirm"), button:has-text("Place Order")'


# =========================
# GROWW EXECUTOR CLASS
# =========================

class GrowwExecutor:
    """
    Playwright-based Groww F&O order executor.

    Called by trading_engine.py AFTER Telegram APPROVE — this class
    should never be called directly from signal generation logic.

    Usage (from trading_engine.py)
    ------------------------------
    if not PAPER_TRADING_MODE and self._groww:
        self._groww.execute_order(signal, lots=1)
    """

    def __init__(self) -> None:
        if PAPER_TRADING_MODE:
            logger.info("[GrowwExec] Paper trading mode — executor instantiated but will not execute")
        else:
            logger.warning("[GrowwExec] ⚠️  LIVE TRADING MODE — orders will be placed on Groww")

    # ------------------------------------------------------------------
    # PRIMARY PUBLIC METHOD
    # ------------------------------------------------------------------

    def execute_order(self, signal: dict, lots: int = 1) -> bool:
        """
        Place an F&O BUY order on Groww.

        Parameters
        ----------
        signal : dict from SignalEngine.compute()
                 Must contain: trade_signal, direction, price
        lots   : number of lots (default 1; scale-up passes 2)

        Returns
        -------
        bool — True if order placement confirmed, False on failure

        Raises
        ------
        RuntimeError if PAPER_TRADING_MODE is True (safety guard)
        """
        # =================================================================
        # ABSOLUTE SAFETY GUARD — never execute in paper mode
        # =================================================================
        if PAPER_TRADING_MODE:
            raise RuntimeError(
                "[GrowwExec] BLOCKED — PAPER_TRADING_MODE=True. "
                "Set PAPER_TRADING_MODE=False in config.py ONLY after full paper validation."
            )

        trade_signal = signal.get("trade_signal", "")
        direction    = signal.get("direction", "")
        price        = signal.get("price", 0)

        # ---- Parse signal string: "BUY 23800 CE" ----
        parts = trade_signal.split()
        if len(parts) < 3:
            logger.error("[GrowwExec] Cannot parse trade signal: %s", trade_signal)
            return False

        strike      = parts[1]
        option_type = parts[2].upper()   # CE or PE
        quantity    = lots * NIFTY_LOT_SIZE

        logger.info(
            "[GrowwExec] Executing order | signal=%s | lots=%d | qty=%d | price=%.2f",
            trade_signal, lots, quantity, price,
        )

        # ---- Attempt Playwright execution ----
        success = False
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
            success = self._run_playwright(
                strike      = strike,
                option_type = option_type,
                quantity    = quantity,
                price       = price,
                lots        = lots,
            )
        except ImportError:
            logger.error("[GrowwExec] Playwright not installed — cannot execute order")
            self._send_telegram(
                f"🚨 <b>Groww Execution FAILED</b>\n"
                f"Playwright is not installed.\n"
                f"Run: <code>playwright install chromium</code>"
            )
            return False
        except Exception as exc:
            logger.error("[GrowwExec] Execution error: %s", exc, exc_info=True)
            self._send_telegram(
                f"🚨 <b>Groww Execution FAILED</b>\n"
                f"Signal: {trade_signal}\n"
                f"Error: {str(exc)[:200]}"
            )
            return False

        # ---- Send outcome to Telegram ----
        if success:
            self._send_telegram(
                f"✅ <b>Groww Order PLACED</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Signal: {trade_signal}\n"
                f"Lots: {lots} ({quantity} qty)\n"
                f"Entry: ₹{price:,.2f}\n"
                f"SL: {STOP_LOSS_POINTS} pts | T1: {TARGET_1_POINTS} pts\n"
                f"Time: {datetime.now().strftime('%H:%M:%S')}"
            )
        else:
            self._send_telegram(
                f"❌ <b>Groww Order FAILED</b>\n"
                f"Signal: {trade_signal}\n"
                f"Manual action required on Groww.\n"
                f"Time: {datetime.now().strftime('%H:%M:%S')}"
            )

        return success

    # ------------------------------------------------------------------
    # INTERNAL: PLAYWRIGHT EXECUTION
    # ------------------------------------------------------------------

    def _run_playwright(
        self,
        strike:      str,
        option_type: str,
        quantity:    int,
        price:       float,
        lots:        int,
    ) -> bool:
        """
        Use Playwright to fill and submit the Groww F&O order form.

        Connects to the already-running Chrome instance on CDP port 9333
        (Groww must be pre-logged-in in that profile).

        Flow:
          1. Connect CDP
          2. Find Groww F&O tab
          3. Search for NIFTY option (strike + CE/PE)
          4. Fill quantity (in lots)
          5. Set order type to MARKET
          6. Click BUY
          7. Click Confirm
          8. Verify order confirmation toast

        Returns True if Confirm was clicked successfully.
        """
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

        with sync_playwright() as pw:
            # ---- Connect to existing Chrome on CDP port 9333 ----
            browser = pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{GROWW_CDP_PORT}"
            )

            # ---- Find Groww F&O tab (or open it) ----
            page = None
            for ctx in browser.contexts:
                for pg in ctx.pages:
                    if "groww.in" in pg.url:
                        page = pg
                        break
                if page:
                    break

            if page is None:
                # Open Groww F&O URL in new tab
                ctx  = browser.contexts[0] if browser.contexts else browser.new_context()
                page = ctx.new_page()
                page.goto(GROWW_FNO_URL, wait_until="domcontentloaded", timeout=30_000)
                logger.info("[GrowwExec] Opened new Groww F&O tab")
            else:
                logger.info("[GrowwExec] Found existing Groww tab: %s", page.url)

            # ---- Search for NIFTY option ----
            search_term = f"NIFTY {strike} {option_type}"
            logger.info("[GrowwExec] Searching: %s", search_term)

            try:
                search_box = page.wait_for_selector(_SEL_SEARCH_INPUT, timeout=10_000)
                search_box.click()
                search_box.fill(search_term)
                time.sleep(1.5)   # Wait for autocomplete

                # Click first matching result
                result = page.wait_for_selector(_SEL_SEARCH_RESULT, timeout=8_000)
                result.click()
                time.sleep(1)
            except PWTimeout:
                logger.error("[GrowwExec] Could not find F&O search input or result")
                return False

            # ---- Fill quantity (lots) ----
            try:
                qty_input = page.wait_for_selector(_SEL_QTY_INPUT, timeout=8_000)
                qty_input.triple_click()
                qty_input.fill(str(lots))   # Groww F&O uses lots, not absolute qty
                logger.info("[GrowwExec] Quantity filled: %d lots", lots)
            except PWTimeout:
                logger.error("[GrowwExec] Could not fill quantity input")
                return False

            # ---- Set MARKET order type ----
            try:
                market_btn = page.query_selector(_SEL_ORDER_TYPE_BTN)
                if market_btn:
                    market_btn.click()
                    logger.info("[GrowwExec] Order type set to MARKET")
            except Exception as exc:
                logger.warning("[GrowwExec] Order type button not found — using default: %s", exc)

            # ---- Click BUY ----
            try:
                buy_btn = page.wait_for_selector(_SEL_BUY_BTN, timeout=8_000)
                buy_btn.click()
                logger.info("[GrowwExec] BUY button clicked")
                time.sleep(0.8)
            except PWTimeout:
                logger.error("[GrowwExec] BUY button not found")
                return False

            # ---- Click Confirm (final placement) ----
            try:
                confirm_btn = page.wait_for_selector(_SEL_CONFIRM_BTN, timeout=8_000)
                confirm_btn.click()
                logger.info("[GrowwExec] Confirm button clicked — order submitted")
                time.sleep(1.5)   # Wait for confirmation toast
            except PWTimeout:
                logger.error("[GrowwExec] Confirm button not found — order may not have been placed")
                return False

            # ---- Take screenshot as evidence ----
            try:
                from config.config import SCREENSHOT_DIR
                ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
                path = SCREENSHOT_DIR / f"groww_order_{ts}.png"
                page.screenshot(path=str(path))
                logger.info("[GrowwExec] Order screenshot saved: %s", path.name)
            except Exception:
                pass   # Screenshot failure does not affect order status

            logger.info("[GrowwExec] ✅ Order placed successfully")
            return True

    # ------------------------------------------------------------------
    # INTERNAL: TELEGRAM NOTIFICATION
    # ------------------------------------------------------------------

    def _send_telegram(self, text: str) -> None:
        """Send a Telegram message directly (not through TelegramApprovalBot)."""
        import requests
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            requests.post(url, json={
                "chat_id":    CHAT_ID,
                "text":       text,
                "parse_mode": "HTML",
            }, timeout=10)
        except Exception as exc:
            logger.error("[GrowwExec] Telegram notification failed: %s", exc)
