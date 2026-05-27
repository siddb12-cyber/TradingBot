"""
automation/groww_connection_test.py
====================================
Groww Playwright Connection Tester.

Connects to Chrome remote debugging on port 9333.
Detects open Groww tabs, prints URL / title / order modal status.
Saves a screenshot of each Groww tab.
READ-ONLY — does not click, type, or interact with any element.

Usage:
    1. Open Chrome with remote debugging enabled on port 9333:
       chrome.exe --remote-debugging-port=9333
    2. Navigate to Groww in that Chrome window
    3. Run: python automation/groww_connection_test.py

Output:
    Console report + screenshots saved to screenshots/groww_test/
"""

import sys
import logging
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

# =========================
# CONFIGURATION
# =========================

GROWW_DEBUG_URL   = "http://127.0.0.1:9333"   # Chrome remote debugging port for Groww
GROWW_DOMAIN      = "groww.in"                 # Used to detect Groww tabs
SCREENSHOT_OUTDIR = Path(__file__).parent.parent / "screenshots" / "groww_test"

# CSS selectors to detect the order placement modal on Groww
# These match the Buy/Sell modal that appears when placing an F&O order.
ORDER_MODAL_SELECTORS = [
    '[class*="orderModal"]',         # class-name approach
    '[class*="order-modal"]',        # hyphen variant
    '[data-testid="order-modal"]',   # testid approach
    'div[class*="BuySellModal"]',    # component name variant
    'div[class*="placeOrder"]',      # place order panel
    'button[class*="buyButton"]',    # buy button inside modal
    'button[class*="sellButton"]',   # sell button inside modal
]

# =========================
# LOGGING
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

SEP = "-" * 52


# =========================
# HELPERS
# =========================

def is_groww_tab(url: str) -> bool:
    """Return True if the page URL belongs to Groww."""
    return GROWW_DOMAIN in url


def detect_order_modal(page) -> tuple[bool, str]:
    """
    Check whether a Groww order modal is currently visible on the page.
    READ-ONLY — only uses locator.count(), no clicks.

    Returns:
        (detected: bool, matched_selector: str)
    """
    for selector in ORDER_MODAL_SELECTORS:
        try:
            count = page.locator(selector).count()
            if count > 0:
                return True, selector
        except Exception:
            continue
    return False, ""


def save_screenshot(page, tab_index: int) -> Path:
    """
    Save a screenshot of the given page to SCREENSHOT_OUTDIR.
    Returns the saved path.
    """
    SCREENSHOT_OUTDIR.mkdir(parents=True, exist_ok=True)
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"groww_tab{tab_index}_{ts}.png"
    outpath  = SCREENSHOT_OUTDIR / filename

    try:
        page.screenshot(path=str(outpath), full_page=False)
        logger.info(f"Screenshot saved: {outpath}")
    except Exception as e:
        logger.error(f"Screenshot failed: {e}")
        outpath = None

    return outpath


# =========================
# MAIN
# =========================

def run():
    logger.info(SEP)
    logger.info("GROWW CONNECTION TESTER")
    logger.info(f"Debug URL : {GROWW_DEBUG_URL}")
    logger.info(f"Domain    : {GROWW_DOMAIN}")
    logger.info("Mode      : READ-ONLY (no clicks)")
    logger.info(SEP)

    with sync_playwright() as p:

        # --- Connect to Chrome ---
        try:
            browser = p.chromium.connect_over_cdp(GROWW_DEBUG_URL)
            logger.info(f"[CHROME] Connected | contexts={len(browser.contexts)}")
        except Exception as e:
            logger.critical(
                f"[CHROME] Cannot connect to {GROWW_DEBUG_URL}: {e}\n"
                "Ensure Chrome is running with: --remote-debugging-port=9333"
            )
            sys.exit(1)

        # --- Gather all pages across all contexts ---
        all_pages = []
        for ctx in browser.contexts:
            all_pages.extend(ctx.pages)

        logger.info(f"[CHROME] Total open tabs: {len(all_pages)}")
        logger.info(SEP)

        if not all_pages:
            logger.warning("[CHROME] No open tabs found.")
            return

        # --- Identify Groww tabs ---
        groww_tabs = []
        for page in all_pages:
            try:
                url = page.url
            except Exception:
                url = ""
            if is_groww_tab(url):
                groww_tabs.append(page)

        logger.info(f"[GROWW] Groww tabs found: {len(groww_tabs)}")

        if not groww_tabs:
            logger.warning(
                f"[GROWW] No tabs matching '{GROWW_DOMAIN}' detected.\n"
                "Open Groww in the Chrome window connected on port 9333."
            )
            logger.info(SEP)
            logger.info("All open tab URLs:")
            for i, page in enumerate(all_pages):
                try:
                    logger.info(f"  [{i}] {page.url}")
                except Exception:
                    logger.info(f"  [{i}] <unreadable>")
            return

        # --- Report each Groww tab ---
        for idx, page in enumerate(groww_tabs):
            logger.info(SEP)
            logger.info(f"GROWW TAB [{idx + 1}/{len(groww_tabs)}]")

            # URL
            try:
                url = page.url
            except Exception as e:
                url = f"<error: {e}>"
            logger.info(f"  URL   : {url}")

            # Title
            try:
                title = page.title()
            except Exception as e:
                title = f"<error: {e}>"
            logger.info(f"  Title : {title}")

            # Order modal detection
            try:
                modal_detected, matched = detect_order_modal(page)
                if modal_detected:
                    logger.info(f"  Order Modal : DETECTED (selector: {matched})")
                else:
                    logger.info("  Order Modal : Not detected")
            except Exception as e:
                logger.warning(f"  Order Modal : Check failed — {e}")

            # Screenshot
            shot_path = save_screenshot(page, idx + 1)
            if shot_path:
                logger.info(f"  Screenshot  : {shot_path}")

        logger.info(SEP)
        logger.info("Test complete. No interactions performed.")
        logger.info(SEP)


if __name__ == "__main__":
    run()
