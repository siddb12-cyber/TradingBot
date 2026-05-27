"""
tools/ocr_debug.py
==================
Diagnostic tool — shows exactly what Tesseract reads from a live TradingView screenshot.

Run this while TradingView is open and the NIFTY chart with VWAP + EMA9 is visible.

Usage:
    python -m tools.ocr_debug

What it does:
    1. Takes a fresh screenshot of TradingView (via CDP on port 9222)
    2. Saves the screenshot to temp/ocr_debug_raw.png
    3. Saves cropped price region to temp/ocr_debug_price_crop.png
    4. Saves cropped indicator region to temp/ocr_debug_indicator_crop.png
    5. Prints the raw Tesseract text from each region
    6. Runs the full OCR pipeline and prints extracted values

Look at the raw text output to understand what patterns need updating.
"""

import sys
import logging
import time
from pathlib import Path

# ── ensure project root is on path ────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))

import cv2
import pytesseract
import numpy as np
from playwright.sync_api import sync_playwright

from config.config import (
    TESSERACT_CMD,
    TESSERACT_CONFIG_TEXT,
    TESSERACT_CONFIG_NUMERIC,
    OCR_PRICE_REGION,
    OCR_INDICATOR_REGION,
    OCR_UPSCALE_FACTOR,
    TRADINGVIEW_CDP_PORT,
    TEMP_DIR,
)
from extraction.ocr_engine import extract_market_values, _preprocess_region, _crop_region

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s | %(name)s | %(message)s")
pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

SEP = "=" * 65


def take_screenshot_via_cdp() -> Path:
    """Connect to running TradingView Chrome and take a screenshot."""
    endpoint = f"http://127.0.0.1:{TRADINGVIEW_CDP_PORT}"
    out_path = TEMP_DIR / "ocr_debug_raw.png"

    print(f"\n[DEBUG] Connecting to Chrome CDP at {endpoint} ...")
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(endpoint)
        context = browser.contexts[0]
        pages = context.pages

        # Find the TradingView page
        tv_page = None
        for pg in pages:
            if "tradingview" in pg.url.lower():
                tv_page = pg
                break

        if tv_page is None:
            tv_page = pages[0]
            print(f"[DEBUG] No TradingView tab found — using first tab: {tv_page.url}")
        else:
            print(f"[DEBUG] Found TradingView tab: {tv_page.url}")

        # Wait a moment for chart to settle
        tv_page.wait_for_timeout(1500)

        # Take screenshot
        tv_page.screenshot(path=str(out_path))
        print(f"[DEBUG] Screenshot saved: {out_path}")

        browser.close()

    return out_path


def show_region_text(image: np.ndarray, region: tuple, label: str, config: str) -> str:
    """Crop, preprocess, and show raw Tesseract text for a region."""
    crop = _crop_region(image, region)
    proc = _preprocess_region(crop)

    # Save cropped image
    crop_path = TEMP_DIR / f"ocr_debug_{label.lower().replace(' ', '_')}_crop.png"
    proc_path  = TEMP_DIR / f"ocr_debug_{label.lower().replace(' ', '_')}_processed.png"
    cv2.imwrite(str(crop_path), crop)
    cv2.imwrite(str(proc_path), proc)
    print(f"[DEBUG] {label} crop saved: {crop_path}")
    print(f"[DEBUG] {label} processed saved: {proc_path}")

    text = pytesseract.image_to_string(proc, config=config)
    return text


def main():
    print(f"\n{SEP}")
    print("  TradingBot — OCR Debug Tool")
    print(f"{SEP}")

    # ── Step 1: Take screenshot ──────────────────────────────────────────────
    try:
        screenshot_path = take_screenshot_via_cdp()
    except Exception as e:
        print(f"\n[ERROR] Could not take screenshot via CDP: {e}")
        print("\nMake sure TradingView Chrome is running (start.bat launched it).")
        print("Alternatively, take a screenshot manually and save it to:")
        print(f"  {TEMP_DIR / 'ocr_debug_raw.png'}")
        print("Then re-run this script — it will use the existing file.")
        screenshot_path = TEMP_DIR / "ocr_debug_raw.png"
        if not screenshot_path.exists():
            sys.exit(1)

    # ── Step 2: Load image ───────────────────────────────────────────────────
    image = cv2.imread(str(screenshot_path))
    if image is None:
        print(f"[ERROR] Could not load image: {screenshot_path}")
        sys.exit(1)

    h, w = image.shape[:2]
    print(f"\n[DEBUG] Image size: {w}x{h} pixels")
    print(f"[DEBUG] Price region: {OCR_PRICE_REGION} (y1,y2,x1,x2)")
    print(f"[DEBUG] Indicator region: {OCR_INDICATOR_REGION} (y1,y2,x1,x2)")

    # ── Step 3: Price region ─────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  PRICE REGION — Raw Tesseract Text")
    print(f"{SEP}")
    price_text = show_region_text(image, OCR_PRICE_REGION, "price", TESSERACT_CONFIG_NUMERIC)
    print(price_text)
    print(f"[End of price text — {len(price_text)} chars]")

    # ── Step 4: Indicator region ─────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  INDICATOR REGION — Raw Tesseract Text")
    print(f"{SEP}")
    indicator_text = show_region_text(image, OCR_INDICATOR_REGION, "indicator", TESSERACT_CONFIG_TEXT)
    print(indicator_text)
    print(f"[End of indicator text — {len(indicator_text)} chars]")

    # ── Step 5: Full pipeline result ─────────────────────────────────────────
    print(f"\n{SEP}")
    print("  FULL OCR PIPELINE RESULT")
    print(f"{SEP}")
    result = extract_market_values(screenshot_path)
    print(f"  current_price : {result['current_price']}")
    print(f"  vwap          : {result['vwap']}")
    print(f"  ema9          : {result['ema9']}")
    print(f"  valid         : {result['valid']}")
    print(f"  error         : {result['error']}")

    # ── Step 6: Guidance ─────────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  NEXT STEPS")
    print(f"{SEP}")
    if result['current_price']:
        print(f"  [OK] Price extraction working: {result['current_price']}")
    else:
        print("  [FAIL] Price not extracted — check price region crop coordinates")

    if result['vwap']:
        print(f"  [OK] VWAP extraction working: {result['vwap']}")
    else:
        print("  [FAIL] VWAP not found — look at the indicator raw text above")
        print("         Check if 'VWAP' appears in the indicator text")
        print("         If not, the crop region may need adjustment")

    if result['ema9']:
        print(f"  [OK] EMA9 extraction working: {result['ema9']}")
    else:
        print("  [FAIL] EMA9 not found — look at the indicator raw text above")
        print("         Check if 'EMA' appears in the indicator text")

    print(f"\n  Cropped images saved to: {TEMP_DIR}")
    print("  Open the *_crop.png and *_processed.png files to see what")
    print("  OCR is actually reading from your chart.\n")


if __name__ == "__main__":
    main()
