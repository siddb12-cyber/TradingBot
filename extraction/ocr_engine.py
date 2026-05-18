"""
extraction/ocr_engine.py
========================
OCR Preprocessing Pipeline for TradingBot.

Responsibility:
    Take a screenshot path -> return validated, structured market values.
    Nothing else. Callers should not perform their own OCR.

Pipeline stages:
    1. Load screenshot with OpenCV
    2. Crop to defined region (from config)
    3. Convert to grayscale
    4. Apply Gaussian denoise
    5. Auto-detect dark/light theme -> apply adaptive threshold
    6. Upscale 2.5x with INTER_CUBIC for sharper character edges
    7. Run Tesseract with PSM 6 / OEM 3
    8. Parse numeric values and text patterns with regex
    9. Validate plausibility (NIFTY range, indicator deviation)
    10. Return typed dict -- always. Never raises to caller.

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
pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

OcrResult = dict


# =========================
# INTERNAL HELPERS
# =========================

def _load_image(screenshot_path) -> Optional[np.ndarray]:
    path_str = str(screenshot_path)
    image = cv2.imread(path_str)
    if image is None:
        logger.error(f"[OCR] Could not load image: {path_str}")
        return None
    logger.debug(f"[OCR] Loaded image: {path_str} | shape={image.shape}")
    return image


def _crop_region(image: np.ndarray, region: tuple) -> np.ndarray:
    y1, y2, x1, x2 = region
    h, w = image.shape[:2]
    y1 = max(0, min(y1, h))
    y2 = max(0, min(y2, h))
    x1 = max(0, min(x1, w))
    x2 = max(0, min(x2, w))
    return image[y1:y2, x1:x2]


def _is_dark_theme(gray_image: np.ndarray) -> bool:
    mean_val = float(np.mean(gray_image))
    is_dark  = mean_val < 128
    logger.debug(f"[OCR] Theme detection | mean_pixel={mean_val:.1f} | dark_theme={is_dark}")
    return is_dark


def _preprocess_region(crop: np.ndarray, scale: float = OCR_UPSCALE_FACTOR) -> np.ndarray:
    """Full preprocessing: grayscale -> denoise -> adaptive threshold -> upscale."""
    gray     = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    denoised = cv2.GaussianBlur(gray, (3, 3), 0)
    dark     = _is_dark_theme(gray)
    thresh_type = cv2.THRESH_BINARY_INV if dark else cv2.THRESH_BINARY
    thresholded = cv2.adaptiveThreshold(
        denoised, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        thresh_type, blockSize=15, C=4,
    )
    kernel  = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    cleaned = cv2.morphologyEx(thresholded, cv2.MORPH_OPEN, kernel)
    h, w    = cleaned.shape
    upscaled = cv2.resize(
        cleaned, (int(w * scale), int(h * scale)),
        interpolation=cv2.INTER_CUBIC,
    )
    return upscaled


def _parse_floats(text: str) -> list:
    """
    Extract valid decimal numbers from raw OCR text.

    Handles two Tesseract misread patterns for Indian price format:

    Case 1 - Comma intact:    "23,604.00" -> 23604.00  (pass 2)
    Case 2 - Comma as period: "23.604.00" -> 23604.00  (pass 1 reconstruction)

    TradingView renders NIFTY prices as "23,604.00". Tesseract frequently
    outputs the thousands comma as a period, producing "23.604.00", which
    the standard regex reads as 23.604 -- outside the 15000-35000 valid range.

    Pass 1 detects the XX.XXX.XX pattern and reconstructs the correct value.
    Pass 2 handles the standard comma-separated format as a fallback.
    """
    values = []
    seen   = set()

    # --- Pass 1: reconstruct period-as-thousands-separator ---
    # Pattern: XX.XXX.XX  e.g. 23.604.00 -> 23604.00
    #          XX.XXX.YY  e.g. 24.156.30 -> 24156.30
    for m in re.finditer(r'\b(\d{1,2})\.(\d{3})\.(\d{2})\b', text):
        try:
            val = float(m.group(1) + m.group(2) + '.' + m.group(3))
            key = round(val, 2)
            if key not in seen:
                seen.add(key)
                values.append(val)
                logger.debug(
                    "[OCR] Reconstructed price: '%s' -> %.2f",
                    m.group(0), val
                )
        except ValueError:
            continue

    # --- Pass 2: standard comma-formatted or plain decimals ---
    for m in re.findall(r'[\d,]+\.\d+', text):
        try:
            val = float(m.replace(',', ''))
            key = round(val, 2)
            if key not in seen:
                seen.add(key)
                values.append(val)
        except ValueError:
            continue

    return values


# =========================
# PRICE EXTRACTION
# =========================

def _extract_price(image: np.ndarray):
    """
    Extract current NIFTY price from the top price bar region.
    Returns (price_float_or_None, raw_ocr_text).
    """
    crop   = _crop_region(image, OCR_PRICE_REGION)
    proc   = _preprocess_region(crop)
    text   = pytesseract.image_to_string(proc, config=TESSERACT_CONFIG_NUMERIC)

    logger.debug("[OCR] Price region raw text:\n%s", text.strip())

    values = _parse_floats(text)

    for val in values:
        if NIFTY_PRICE_MIN <= val <= NIFTY_PRICE_MAX:
            logger.debug("[OCR] Detected current_price=%.2f", val)
            return val, text

    logger.warning("[OCR] No valid price found in price region. Values found: %s", values)
    return None, text


# =========================
# INDICATOR EXTRACTION
# =========================

def _extract_indicators(image: np.ndarray):
    """
    Extract VWAP and EMA9 from the indicator legend in the top-left of the chart.
    Returns (vwap_or_None, ema9_or_None, raw_ocr_text).
    """
    crop = _crop_region(image, OCR_INDICATOR_REGION)
    proc = _preprocess_region(crop)
    text = pytesseract.image_to_string(proc, config=TESSERACT_CONFIG_TEXT)

    logger.debug("[OCR] Indicator region raw text:\n%s", text.strip())

    vwap = None
    ema9 = None

    # --- VWAP patterns ---
    # TradingView legend formats seen in practice:
    #   "VWAP (Session, SMA, 0)  25,023.45"
    #   "VWAP Session SMA 0 25023.45"
    #   "WAP Session 25023.45"   <- OCR drops leading V
    #   "VWAP 25023.45"
    vwap_patterns = [
        r'VWAP\s*\([^)]*\)\s*([\d,.]+)',              # VWAP (...) VALUE
        r'VWAP\s+Session[^\d]*([\d,.]+)',              # VWAP Session VALUE
        r'WAP\s+Session[^\d]*([\d,.]+)',               # WAP Session VALUE (OCR drops V)
        r'\bSession\b[^\d\n]{0,20}([\d,.]{6,})',      # Session VALUE — VWAP garbled but Session readable
        r'wer\s+Session[^\d]*([\d,.]+)',               # wer Session VALUE (OCR-specific garble of VWAP)
        r'VWAP\s*[^\d\n]{0,40}([\d,.]{6,})',          # VWAP ... VALUE (>=6 chars)
        r'WAP\s*[^\d\n]{0,40}([\d,.]{6,})',           # WAP ... VALUE
        r'VW[^\d\n]{0,60}(2[0-9][,.]?\d{3}[\d,.]*)', # VW* followed by 2X,XXX-range price
    ]
    for pattern in vwap_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            raw = match.group(1)
            candidates = _parse_floats(raw + '.00') or _parse_floats(raw)
            for c in candidates:
                if NIFTY_PRICE_MIN <= c <= NIFTY_PRICE_MAX:
                    vwap = c
                    logger.debug("[OCR] Detected vwap=%.2f via pattern: %s", vwap, pattern)
                    break
            if vwap is not None:
                break

    if vwap is None:
        logger.warning("[OCR] VWAP not found in indicator region.")

    # --- EMA9 patterns ---
    # TradingView legend formats seen in practice:
    #   "EMA (Close, 9)  24,987.23"
    #   "EMA · Close · 9  24987.23"
    #   "EMA Close 9 24987.23"
    #   "EMA9 24987.23"
    ema_patterns = [
        r'EMA\s*\([^)]*9[^)]*\)\s*([\d,.]+)',        # EMA (...9...) VALUE
        r'EMA\s*\([^)]*[Cc]lose[^)]*\)\s*([\d,.]+)', # EMA (...close...) VALUE
        r'EMA\s*9?\s*[·•\-,]?\s*[Cc]lose[^\d]*([\d,.]+)', # EMA 9·Close VALUE
        r'EMA\s*[Cc]lose[^\d]*([\d,.]+)',             # EMA Close VALUE
        r'EMA\s*9[^\d]*([\d,.]+)',                    # EMA9 VALUE
        r'\bEMA\b[^\d\n]{0,60}(2[0-9][,.]?\d{3}[\d,.]*)', # EMA ... 2X,XXX-range
    ]
    for pattern in ema_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            raw = match.group(1)
            candidates = _parse_floats(raw + '.00') or _parse_floats(raw)
            for c in candidates:
                if NIFTY_PRICE_MIN <= c <= NIFTY_PRICE_MAX:
                    ema9 = c
                    logger.debug("[OCR] Detected ema9=%.2f via pattern: %s", ema9, pattern)
                    break
            if ema9 is not None:
                break

    if ema9 is None:
        logger.warning("[OCR] EMA9 not found in indicator region.")

    return vwap, ema9, text


# =========================
# VALIDATION
# =========================

def validate_market_values(price, vwap, ema9) -> dict:
    """
    Validate that extracted values are plausible for NIFTY.
    Returns {"valid": bool, "reason": str}.
    """
    if price is None:
        return {"valid": False, "reason": "Current price could not be extracted"}

    if not (NIFTY_PRICE_MIN <= price <= NIFTY_PRICE_MAX):
        return {
            "valid": False,
            "reason": (
                "Price %.2f is outside the valid NIFTY range "
                "[%.0f - %.0f]" % (price, NIFTY_PRICE_MIN, NIFTY_PRICE_MAX)
            ),
        }

    if vwap is not None and abs(price - vwap) > NIFTY_INDICATOR_MAX_DEVIATION:
        return {
            "valid": False,
            "reason": (
                "VWAP %.2f deviates %.0f pts from price %.2f "
                "(max allowed: %.0f pts)" % (
                    vwap, abs(price - vwap), price, NIFTY_INDICATOR_MAX_DEVIATION
                )
            ),
        }

    if ema9 is not None and abs(price - ema9) > NIFTY_INDICATOR_MAX_DEVIATION:
        return {
            "valid": False,
            "reason": (
                "EMA9 %.2f deviates %.0f pts from price %.2f "
                "(max allowed: %.0f pts)" % (
                    ema9, abs(price - ema9), price, NIFTY_INDICATOR_MAX_DEVIATION
                )
            ),
        }

    return {"valid": True, "reason": "OK"}


# =========================
# PUBLIC API
# =========================

def extract_market_values(screenshot_path) -> OcrResult:
    """
    Main entry point. Load screenshot, run OCR pipeline, return structured result.
    Never raises -- always returns a dict with "valid" key.
    """
    result: OcrResult = {
        "current_price":      None,
        "vwap":               None,
        "ema9":               None,
        "valid":              False,
        "error":              None,
        "raw_price_text":     "",
        "raw_indicator_text": "",
    }

    try:
        image = _load_image(screenshot_path)
        if image is None:
            result["error"] = "Could not load screenshot"
            return result

        price, price_text = _extract_price(image)
        result["current_price"]  = price
        result["raw_price_text"] = price_text.strip()

        vwap, ema9, indicator_text = _extract_indicators(image)
        result["vwap"]                = vwap
        result["ema9"]                = ema9
        result["raw_indicator_text"]  = indicator_text.strip()

        # ==============================================
        # OHLC BAR CROSS-CHECK (always runs)
        # The dedicated price region OCR is unreliable for this screen layout —
        # it reads toolbar buttons and produces garbage that can still fall inside
        # the 15,000–35,000 NIFTY range (e.g. "32029.50" from pixel noise).
        #
        # The OHLC bar in the indicator region reads correctly every time.
        # Strategy:
        #   • If price region failed → use OHLC bar (fallback).
        #   • If price region succeeded but differs from OHLC bar by >500 pts
        #     → the price region value is a false positive; prefer OHLC bar.
        #   • If both agree → keep price region value (no change).
        # ==============================================
        if indicator_text:
            ohlc_candidates = _parse_floats(indicator_text)
            ohlc_price = None
            for c in ohlc_candidates:
                if NIFTY_PRICE_MIN <= c <= NIFTY_PRICE_MAX:
                    ohlc_price = c
                    break

            if ohlc_price is not None:
                if price is None:
                    # Simple fallback
                    price = ohlc_price
                    result["current_price"] = price
                    logger.info("[OCR] Price from OHLC bar (fallback): %.2f", price)
                elif abs(price - ohlc_price) > 500:
                    # Price region reading is a false positive — OHLC bar is authoritative
                    logger.warning(
                        "[OCR] Price region (%.2f) vs OHLC bar (%.2f) differ by %.0f pts "
                        "— price region is unreliable, using OHLC bar value",
                        price, ohlc_price, abs(price - ohlc_price),
                    )
                    price = ohlc_price
                    result["current_price"] = price
                # else: both agree — keep price region value
            elif price is None:
                logger.warning("[OCR] OHLC bar also failed — no NIFTY-range value in indicator text")

        validation      = validate_market_values(price, vwap, ema9)
        result["valid"] = validation["valid"]

        if not validation["valid"]:
            result["error"] = validation["reason"]
            logger.warning("[OCR] Validation failed: %s", validation["reason"])
        else:
            logger.info(
                "[OCR] Extracted | price=%.2f vwap=%s ema9=%s",
                price,
                ("%.2f" % vwap) if vwap else "None",
                ("%.2f" % ema9) if ema9 else "None",
            )

        return result

    except Exception as exc:
        result["error"] = str(exc)
        logger.error("[OCR] Unexpected error in extract_market_values: %s", exc)
        return result


def extract_live_price(screenshot_path) -> Optional[float]:
    """
    Lightweight helper -- extract only the current price from a screenshot.
    Used by live_trade_tracker for exit price detection.
    Returns float price or None on failure.
    """
    try:
        image = cv2.imread(str(screenshot_path))
        if image is None:
            logger.error("[OCR] extract_live_price: cannot read %s", screenshot_path)
            return None

        price, _ = _extract_price(image)

        if price is None:
            return None

        if not (NIFTY_PRICE_MIN <= price <= NIFTY_PRICE_MAX):
            logger.debug(
                "[OCR] extract_live_price: price %.2f outside valid range, rejecting", price
            )
            return None

        return price

    except Exception as exc:
        logger.error("[OCR] extract_live_price error: %s", exc)
        return None
