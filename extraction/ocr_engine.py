"""
extraction/ocr_engine.py
========================
OCR Preprocessing Pipeline for TradingBot.

Responsibility:
    Take a screenshot path → return validated, structured market values.
    Nothing else. Callers should not perform their own OCR.

Pipeline stages:
    1. Load screenshot with OpenCV
    2. Crop to defined region (from config)
    3. Convert to grayscale
    4. Apply Gaussian denoise
    5. Auto-detect dark/light theme → apply adaptive threshold
    6. Upscale 2.5x with INTER_CUBIC for sharper character edges
    7. Run Tesseract with PSM 6 / OEM 3
    8. Parse numeric values and text patterns with regex
    9. Validate plausibility (NIFTY range, indicator deviation)
    10. Return typed dict — always. Never raises to caller.

Return shape:
    {
        "current_price": float | None,
        "vwap":          float | None,
        "ema9":          float | None,
        "valid":         bool,
        "error":         str | None,
        "raw_price_text":      str,
        "raw_indicator_text":  str,
    }
"""

import re
import logging
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import pytesseract

from config.config import (
    TESSERACT_CMD,
    TESSERACT_CONFIG_TEXT,
    TESSERACT_CONFIG_NUMERIC,
    OCR_PRICE_REGION,
    OCR_INDICATOR_REGION,
    OCR_UPSCALE_FACTOR,
    NIFTY_PRICE_MIN,
    NIFTY_PRICE_MAX,
    NIFTY_INDICATOR_MAX_DEVIATION,
)

# =========================
# SETUP
# =========================

logger = logging.getLogger(__name__)

# Set Tesseract binary path from config (loaded from .env)
pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

# =========================
# TYPE ALIAS
# =========================

OcrResult = dict  # typed structure defined in module docstring


# =========================
# INTERNAL HELPERS
# =========================

def _load_image(screenshot_path: str | Path) -> Optional[np.ndarray]:
    """
    Load image from disk. Returns None if file is missing or unreadable.
    """
    path_str = str(screenshot_path)
    image = cv2.imread(path_str)

    if image is None:
        logger.error(f"[OCR] Could not load image: {path_str}")
        return None

    logger.debug(f"[OCR] Loaded image: {path_str} | shape={image.shape}")
    return image


def _crop_region(image: np.ndarray, region: tuple) -> np.ndarray:
    """
    Crop image to (y1, y2, x1, x2) region from config.
    Clamps coordinates to image boundaries to avoid index errors.
    """
    y1, y2, x1, x2 = region
    h, w = image.shape[:2]

    y1 = max(0, min(y1, h))
    y2 = max(0, min(y2, h))
    x1 = max(0, min(x1, w))
    x2 = max(0, min(x2, w))

    return image[y1:y2, x1:x2]


def _is_dark_theme(gray_image: np.ndarray) -> bool:
    """
    Determine whether TradingView is using a dark theme.
    Dark theme = mean pixel value below 128.
    This controls which threshold direction we apply.
    """
    mean_val = float(np.mean(gray_image))
    is_dark = mean_val < 128
    logger.debug(f"[OCR] Theme detection | mean_pixel={mean_val:.1f} | dark_theme={is_dark}")
    return is_dark


def _preprocess_region(crop: np.ndarray, scale: float = OCR_UPSCALE_FACTOR) -> np.ndarray:
    """
    Full preprocessing pipeline for a single cropped region.

    Steps:
        1. Convert to grayscale
        2. Gaussian denoise (kernel 3x3)
        3. Adaptive threshold — inverts automatically for dark/light themes
        4. Morphological opening to remove tiny noise specks
        5. Upscale with INTER_CUBIC for sharper character edges

    Returns:
        Processed numpy array ready for Tesseract.
    """
    # --- Step 1: Grayscale ---
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)

    # --- Step 2: Gaussian denoise ---
    denoised = cv2.GaussianBlur(gray, (3, 3), 0)

    # --- Step 3: Adaptive threshold ---
    # ADAPTIVE_THRESH_GAUSSIAN_C weights neighbors by a Gaussian window.
    # More robust than mean-based for charts with varying brightness zones.
    # blockSize=15 captures local context; C=4 fine-tunes the cutoff.
    # For dark themes, text is bright-on-dark → we use THRESH_BINARY_INV
    # so the result is always dark-text-on-white for Tesseract.
    dark = _is_dark_theme(gray)
    thresh_type = cv2.THRESH_BINARY_INV if dark else cv2.THRESH_BINARY

    thresholded = cv2.adaptiveThreshold(
        denoised,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        thresh_type,
        blockSize=15,
        C=4,
    )

    # --- Step 4: Morphological opening — remove isolated noise pixels ---
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    cleaned = cv2.morphologyEx(thresholded, cv2.MORPH_OPEN, kernel)

    # --- Step 5: Upscale ---
    h, w = cleaned.shape
    upscaled = cv2.resize(
        cleaned,
        (int(w * scale), int(h * scale)),
        interpolation=cv2.INTER_CUBIC,
    )

    return upscaled


def _parse_floats(text: str) -> list[float]:
    """
    Extract all valid decimal numbers from raw OCR text.
    Handles comma-formatted numbers like 23,763.97
    """
    raw_matches = re.findall(r'[\d,]+\.\d+', text)
    values = []
    for match in raw_matches:
        try:
            values.append(float(match.replace(",", "")))
        except ValueError:
            continue
    return values


def _extract_price(image: np.ndarray) -> tuple[Optional[float], str]:
    """
    Extract current NIFTY price from the top price bar region.

    Returns:
        (price_float_or_None, raw_ocr_text)
    """
    crop   = _crop_region(image, OCR_PRICE_REGION)
    proc   = _preprocess_region(crop)
    text   = pytesseract.image_to_string(proc, config=TESSERACT_CONFIG_NUMERIC)

    logger.debug(f"[OCR] Price region raw text:\n{text.strip()}")

    values = _parse_floats(text)

    # First value in the valid NIFTY range is the current price.
    for val in values:
        if NIFTY_PRICE_MIN <= val <= NIFTY_PRICE_MAX:
            logger.debug(f"[OCR] Detected current_price={val}")
            return val, text

    logger.warning(f"[OCR] No valid price found in price region. Values found: {values}")
    return None, text


def _extract_indicators(image: np.ndarray) -> tuple[Optional[float], Optional[float], str]:
    """
    Extract VWAP and EMA9 from the indicator display area.

    TradingView renders indicator values in the top-left legend.
    Tesseract often misreads "VWAP" as "WAP" or "VWAP" — we try multiple patterns.

    Returns:
        (vwap_or_None, ema9_or_None, raw_ocr_text)
    """
    crop = _crop_region(image, OCR_INDICATOR_REGION)
    proc = _preprocess_region(crop)
    text = pytesseract.image_to_string(proc, config=TESSERACT_CONFIG_TEXT)

    logger.debug(f"[OCR] Indicator region raw text:\n{text.strip()}")

    vwap: Optional[float] = None
    ema9: Optional[float] = None

    # --- VWAP: try multiple common OCR misread variants ---
    # TradingView label is typically: "VWAP Session  23,763.97"
    vwap_patterns = [
        r'VWAP\s+Session\s+([\d,]+\.\d+)',   # clean read
        r'WAP\s+Session\s+([\d,]+\.\d+)',     # missing V
        r'VWAP[^\d]+([\d,]+\.\d+)',           # any separator
        r'WAP[^\d]+([\d,]+\.\d+)',            # any separator, no V
    ]
    for pattern in vwap_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            try:
                vwap = float(match.group(1).replace(",", ""))
                logger.debug(f"[OCR] Detected vwap={vwap} via pattern: {pattern}")
                break
            except ValueError:
                continue

    if vwap is None:
        logger.warning("[OCR] VWAP not found in indicator region.")

    # --- EMA9: TradingView label is typically: "EMA9 close  23,738.36" ---
    ema_patterns = [
        r'EMA9?\s+close\s+([\d,]+\.\d+)',  # standard
        r'EMA\s*9[^\d]+([\d,]+\.\d+)',     # spacing variants
        r'EMA9[^\d]+([\d,]+\.\d+)',        # no 'close' text
    ]
    for pattern in ema_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            try:
                ema9 = float(match.group(1).replace(",", ""))
                logger.debug(f"[OCR] Detected ema9={ema9} via pattern: {pattern}")
                break
            except ValueError:
                continue

    if ema9 is None:
        logger.warning("[OCR] EMA9 not found in indicator region.")

    return vwap, ema9, text


# =========================
# VALIDATION
# =========================

def validate_market_values(
    price: Optional[float],
    vwap:  Optional[float],
    ema9:  Optional[float],
) -> dict:
    """
    Plausibility gate before any trade decision is made.

    Rules:
    - price must be between NIFTY_PRICE_MIN and NIFTY_PRICE_MAX
    - VWAP must exist and not deviate > NIFTY_INDICATOR_MAX_DEVIATION from price
    - EMA9 must exist and not deviate > NIFTY_INDICATOR_MAX_DEVIATION from price

    Returns:
        {"valid": bool, "reason": str}
    """
    if price is None:
        return {"valid": False, "reason": "Current price could not be extracted"}

    if not (NIFTY_PRICE_MIN <= price <= NIFTY_PRICE_MAX):
        return {
            "valid": False,
            "reason": (
                f"Price {price:.2f} is outside the valid NIFTY range "
                f"[{NIFTY_PRICE_MIN:.0f} – {NIFTY_PRICE_MAX:.0f}]"
            ),
        }

    if vwap is None:
        return {"valid": False, "reason": "VWAP could not be extracted"}

    if abs(price - vwap) > NIFTY_INDICATOR_MAX_DEVIATION:
        return {
            "valid": False,
            "reason": (
                f"VWAP {vwap:.2f} deviates {abs(price - vwap):.1f} pts from "
                f"price {price:.2f} — likely an OCR error "
                f"(max allowed: {NIFTY_INDICATOR_MAX_DEVIATION:.0f} pts)"
            ),
        }

    if ema9 is None:
        return {"valid": False, "reason": "EMA9 could not be extracted"}

    if abs(price - ema9) > NIFTY_INDICATOR_MAX_DEVIATION:
        return {
            "valid": False,
            "reason": (
                f"EMA9 {ema9:.2f} deviates {abs(price - ema9):.1f} pts from "
                f"price {price:.2f} — likely an OCR error "
                f"(max allowed: {NIFTY_INDICATOR_MAX_DEVIATION:.0f} pts)"
            ),
        }

    return {"valid": True, "reason": "All values passed validation"}


# =========================
# PRIMARY PUBLIC FUNCTION
# =========================

def extract_market_values(screenshot_path: str | Path) -> OcrResult:
    """
    Main entry point for all OCR extraction.

    Usage:
        from extraction.ocr_engine import extract_market_values

        result = extract_market_values(screenshot_path)
        if not result["valid"]:
            logger.warning(f"Skipping cycle: {result['error']}")
            continue

        price = result["current_price"]
        vwap  = result["vwap"]
        ema9  = result["ema9"]

    Returns:
        OcrResult dict with keys:
            current_price (float|None), vwap (float|None), ema9 (float|None),
            valid (bool), error (str|None),
            raw_price_text (str), raw_indicator_text (str)
    """
    result: OcrResult = {
        "current_price":       None,
        "vwap":                None,
        "ema9":                None,
        "valid":               False,
        "error":               None,
        "raw_price_text":      "",
        "raw_indicator_text":  "",
    }

    try:
        # --- Load image ---
        image = _load_image(screenshot_path)
        if image is None:
            result["error"] = f"Failed to load screenshot: {screenshot_path}"
            return result

        # --- Extract price ---
        price, price_text = _extract_price(image)
        result["current_price"]  = price
        result["raw_price_text"] = price_text.strip()

        # --- Extract indicators ---
        vwap, ema9, indicator_text = _extract_indicators(image)
        result["vwap"]                = vwap
        result["ema9"]                = ema9
        result["raw_indicator_text"]  = indicator_text.strip()

        # --- Validate ---
        validation = validate_market_values(price, vwap, ema9)
        result["valid"] = validation["valid"]

        if not validation["valid"]:
            result["error"] = validation["reason"]
            logger.warning(f"[OCR] Validation failed → {validation['reason']}")
        else:
            logger.info(
                f"[OCR] Extraction successful | "
                f"price={price:.2f}  vwap={vwap:.2f}  ema9={ema9:.2f}"
            )

    except Exception as exc:
        result["error"] = f"Unexpected OCR error: {exc}"
        logger.exception(f"[OCR] Unhandled exception during extraction: {exc}")

    return result


# =========================
# UTILITY: EXTRACT LIVE PRICE ONLY
# =========================

def extract_live_price(screenshot_path: str | Path) -> Optional[float]:
    """
    Lightweight helper for the live tracker — only extracts current price.
    Returns None if extraction or validation fails.

    Usage:
        from extraction.ocr_engine import extract_live_price
        live_price = extract_live_price(screenshot_path)
        if live_price is None:
            # skip this tracker tick
    """
    try:
        image = _load_image(screenshot_path)
        if image is None:
            return None

        price, _ = _extract_price(image)

        if price is None:
            logger.warning("[OCR] extract_live_price: price not found in image")
            return None

        if not (NIFTY_PRICE_MIN <= price <= NIFTY_PRICE_MAX):
            logger.warning(
                f"[OCR] extract_live_price: price {price:.2f} outside valid range, rejecting"
            )
            return None

        return price

    except Exception as exc:
        logger.exception(f"[OCR] extract_live_price error: {exc}")
        return None
