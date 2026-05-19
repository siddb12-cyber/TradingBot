"""
core/multi_timeframe.py
=======================
Multi-Timeframe Confirmation Engine for TradingBot.

Responsibilities:
    1. Switch TradingView chart to each configured timeframe using Playwright
    2. Take screenshot and run OCR pipeline for each timeframe
    3. Aggregate per-TF readings (price, VWAP, EMA9) into a unified analysis
    4. Compute bullish/bearish/mixed alignment across timeframes
    5. Compute a 0-100 confidence score from three weighted components:
         - Timeframe alignment (50 pts max)
         - VWAP proximity / distance (25 pts max)
         - EMA9 alignment consistency (25 pts max)
    6. Classify confidence level: HIGH / MEDIUM / LOW
    7. Return a structured MTFResult dict for use by ai_trading_assistant

Signal direction is always anchored to PRIMARY_TIMEFRAME (5m).
If 5m OCR fails, the entire MTF analysis is aborted.

Timeframe switching approach:
    TradingView toolbar buttons are clicked by their visible text label.
    Playwright uses page.get_by_role("button", name=...) with exact=True.
    After each click we wait TF_WAIT_MS for chart re-render.
    After all TFs are processed, the chart is restored to PRIMARY_TIMEFRAME.

Paper Trading Only.
"""

import logging
import re
import time
from pathlib import Path
from typing import Optional

from config.config import (
    TIMEFRAMES,
    PRIMARY_TIMEFRAME,
    TF_WAIT_MS,
    TF_SELECTOR_MAP,
    CONFIDENCE_HIGH_THRESHOLD,
    CONFIDENCE_MED_THRESHOLD,
    SCORE_WEIGHT_TF_ALIGN,
    SCORE_WEIGHT_VWAP_DIST,
    SCORE_WEIGHT_EMA_ALIGN,
    TEMP_DIR,
    NIFTY_STRIKE_INTERVAL,
    STOP_LOSS_POINTS,
    TARGET_1_POINTS,
    TARGET_2_POINTS,
    TARGET_3_POINTS,
)
from extraction.ocr_engine import extract_market_values

# =========================
# LOGGING
# =========================

logger = logging.getLogger(__name__)

# =========================
# CONFIDENCE LEVEL LABELS
# =========================

CONFIDENCE_HIGH   = "HIGH"
CONFIDENCE_MEDIUM = "MEDIUM"
CONFIDENCE_LOW    = "LOW"

# =========================
# DIRECTION CONSTANTS
# =========================

DIR_BULLISH  = "BULLISH"
DIR_BEARISH  = "BEARISH"
DIR_SIDEWAYS = "SIDEWAYS"

# =========================
# MTFResult TYPE
# =========================
# Returned by MultiTimeframeAnalyzer.analyze()
#
# {
#   "primary_direction": str | None    — "BULLISH" / "BEARISH" / "SIDEWAYS" / None
#   "trade_signal":      str | None    — "BUY 23800 CE" / "BUY 23800 PE" / "NO TRADE"
#   "is_trade":          bool
#   "confidence_score":  int           — 0-100
#   "confidence_level":  str           — "HIGH" / "MEDIUM" / "LOW"
#   "timeframe_data":    dict          — per-TF OCR results
#   "alignment_summary": str           — human-readable e.g. "3/3 BULLISH"
#   "aligned_count":     int           — TFs agreeing with primary direction
#   "total_tf_count":    int           — total TFs processed
#   "error":             str | None    — set if fatal failure occurred
#   "valid":             bool          — False if primary TF OCR failed
# }


# =========================
# INTERNAL HELPERS
# =========================

def _extract_indicators_from_dom(page) -> dict:
    """
    Read VWAP and EMA9 values directly from TradingView's page text.

    More reliable than OCR for the small indicator legend text.
    TradingView renders the legend as HTML — Playwright can read it directly
    without any image processing.

    Called as a fallback when OCR fails to extract VWAP or EMA9.

    Returns: {"vwap": float|None, "ema9": float|None}
    """
    from extraction.ocr_engine import _parse_floats, NIFTY_PRICE_MIN, NIFTY_PRICE_MAX

    result = {"vwap": None, "ema9": None}

    try:
        # Read the full page body text — TradingView renders legend as plain HTML text
        text = page.inner_text("body", timeout=3000)
    except Exception as e:
        logger.debug("[MTF][DOM] Could not read page text: %s", e)
        return result

    if not text:
        return result

    logger.debug("[MTF][DOM] Page text sample (first 800 chars):\n%s", text[:800])

    # ==============================================
    # VWAP — anchor on "Session" keyword
    # TradingView renders: "VWAP Session  23,384.63  23,415.80  23,353.46"
    # "VWAP" may be garbled by OCR, but "Session" is stable.
    # DOM text reads it directly so both words should be intact.
    # ==============================================
    vwap_patterns = [
        r'VWAP\s+Session[^\d]*([\d,]+\.[\d]{2})',   # VWAP Session VALUE
        r'VWAP[^\d\n]{0,60}([\d,]+\.[\d]{2})',       # VWAP ... VALUE
        r'\bSession\b[^\d\n]{0,20}([\d,]+\.[\d]{2})', # Session VALUE (VWAP garbled)
    ]
    for pattern in vwap_patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            candidates = _parse_floats(m.group(1))
            for c in candidates:
                if NIFTY_PRICE_MIN <= c <= NIFTY_PRICE_MAX:
                    result["vwap"] = c
                    logger.info("[MTF][DOM] VWAP extracted: %.2f (pattern: %s)", c, pattern)
                    break
        if result["vwap"] is not None:
            break

    # ==============================================
    # EMA9 — anchor on "close" or "EMA" + digit
    # TradingView renders: "EMA 9 close  23,384.45"
    # ==============================================
    ema_patterns = [
        r'EMA\s*9\s*[Cc]lose[^\d]*([\d,]+\.[\d]{2})', # EMA 9 close VALUE
        r'EMA\s*[Cc]lose[^\d]*([\d,]+\.[\d]{2})',      # EMA close VALUE
        r'EMA\s*9[^\d\n]{0,30}([\d,]+\.[\d]{2})',      # EMA 9 VALUE
        r'\b9\s+[Cc]lose[^\d]*([\d,]+\.[\d]{2})',      # 9 close VALUE
    ]
    for pattern in ema_patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            candidates = _parse_floats(m.group(1))
            for c in candidates:
                if NIFTY_PRICE_MIN <= c <= NIFTY_PRICE_MAX:
                    result["ema9"] = c
                    logger.info("[MTF][DOM] EMA9 extracted: %.2f (pattern: %s)", c, pattern)
                    break
        if result["ema9"] is not None:
            break

    if result["vwap"] is None:
        logger.warning("[MTF][DOM] VWAP not found in page text")
    if result["ema9"] is None:
        logger.warning("[MTF][DOM] EMA9 not found in page text")

    return result


def _tf_direction(price: float, vwap, ema9) -> str:
    """
    Determine bullish / bearish / sideways for a single timeframe.
    Returns SIDEWAYS if price, vwap, or ema9 is None — never raises.
    Matches the same logic used in ai_trading_assistant.decide_signal().
    """
    if price is None or vwap is None or ema9 is None:
        logger.debug(
            "[MTF] _tf_direction: missing value — price=%s vwap=%s ema9=%s → SIDEWAYS",
            price, vwap, ema9,
        )
        return DIR_SIDEWAYS
    if price > vwap and price > ema9:
        return DIR_BULLISH
    elif price < vwap and price < ema9:
        return DIR_BEARISH
    else:
        return DIR_SIDEWAYS


def _dismiss_ads(page) -> int:
    """
    Silently dismiss all known TradingView ad/popup/overlay types.

    Handles:
      - Upgrade / subscription modals ("Maybe later", "No thanks", "Continue for free")
      - Cookie consent banners
      - Toast / notification popups
      - Survey / feedback overlays
      - Generic close buttons (aria-label, data-name, class-based)

    Each selector is tried with a 1.5s timeout so this never blocks the main loop.
    Returns the count of elements successfully dismissed.
    """
    dismissed = 0

    DISMISS_SELECTORS = [
        # Text-based buttons (most reliable across TradingView UI updates)
        'button:has-text("Maybe later")',
        'button:has-text("No, thanks")',
        'button:has-text("No thanks")',
        'button:has-text("Continue for free")',
        'button:has-text("Keep current plan")',
        'button:has-text("Dismiss")',
        'button:has-text("Got it")',
        'button:has-text("Accept")',
        'button:has-text("I agree")',
        'button:has-text("Close")',
        # TradingView-specific data attributes
        '[data-name="close-button"]',
        '[data-role="button"][aria-label="Close"]',
        '[data-dialog-name] [aria-label="Close"]',
        # CSS class patterns used by TradingView dialogs
        '.js-dialog__close',
        '.tv-dialog__close',
        '.close-B02UUUN3',
        '.closeButton-LeHfmr1a',
        # Toast / notification close
        '[data-role="toast-close-button"]',
        # Cookie / GDPR banners
        '#cookie-law-info-bar button',
        # Aria-label fallback
        'button[aria-label="Close"]',
        'button[aria-label="Dismiss"]',
    ]

    for selector in DISMISS_SELECTORS:
        try:
            locator = page.locator(selector).first
            if locator.is_visible(timeout=1500):
                locator.click(timeout=1500)
                dismissed += 1
                logger.debug(f"[ADS] Dismissed: {selector}")
                page.wait_for_timeout(300)
        except Exception:
            pass

    # Keyboard Escape as final fallback for any remaining modal
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass

    if dismissed > 0:
        logger.info(f"[ADS] Dismissed {dismissed} TradingView overlay(s)")

    return dismissed


def _switch_timeframe(page, tf_label: str) -> bool:
    """
    Switch TradingView chart to the specified timeframe.

    Strategy (in order of reliability):
        1. URL navigation — replace interval= parameter in the TradingView URL.
           This is the most reliable approach and does not depend on UI element
           selectors, which break every time TradingView updates its React bundle.
           The browser stays on the same domain so the session cookie is preserved.
        2. data-value attribute click — fallback if navigation fails.
        3. Role-based button click — last resort.

    TradingView URL interval values:
        5m  → interval=5
        15m → interval=15
        1h  → interval=60

    Returns True if the switch succeeded (or probably succeeded), False otherwise.
    """
    import re as _re
    from config.config import TRADINGVIEW_URL

    # Map tf_label → TradingView URL interval value
    interval_map = {
        "5m":  "5",
        "15m": "15",
        "1h":  "60",
    }
    interval = interval_map.get(tf_label)
    if not interval:
        logger.warning("[MTF] No interval mapping for tf=%s", tf_label)
        return False

    # --- Method 1: URL navigation (replace interval= in the configured URL) ---
    try:
        if "interval=" in TRADINGVIEW_URL:
            new_url = _re.sub(r"interval=\d+", "interval=" + interval, TRADINGVIEW_URL)
        else:
            sep     = "&" if "?" in TRADINGVIEW_URL else "?"
            new_url = TRADINGVIEW_URL + sep + "interval=" + interval

        page.goto(new_url, wait_until="domcontentloaded", timeout=20_000)
        logger.info("[MTF] Switched to %s via URL navigation (interval=%s)", tf_label, interval)
        return True
    except Exception as e:
        logger.warning("[MTF] URL navigation failed for %s: %s", tf_label, e)

    # --- Method 2: data-value attribute click ---
    selector_label = TF_SELECTOR_MAP.get(tf_label, interval)
    try:
        locator = page.locator('[data-value="%s"]' % selector_label)
        if locator.count() > 0:
            locator.first.click()
            logger.info("[MTF] Switched to %s via data-value click", tf_label)
            return True
    except Exception as e:
        logger.debug("[MTF] data-value click failed for %s: %s", tf_label, e)

    # --- Method 3: role-based button ---
    try:
        btn = page.get_by_role("button", name=selector_label, exact=True)
        if btn.count() > 0:
            btn.first.click()
            logger.info("[MTF] Switched to %s via role button click", tf_label)
            return True
    except Exception as e:
        logger.debug("[MTF] Role button click failed for %s: %s", tf_label, e)

    logger.warning("[MTF] All switch methods failed for %s", tf_label)
    return False


def _score_tf_alignment(primary_direction: str, tf_results: dict) -> int:
    """
    Score component 1: Timeframe alignment (0 – SCORE_WEIGHT_TF_ALIGN pts).

    Logic:
        - Count how many TFs agree with primary_direction
        - Sideways TFs count as neutral (0.5 agreement weight)
        - Score scales linearly with agreement ratio

    Args:
        primary_direction: DIR_BULLISH or DIR_BEARISH
        tf_results: dict of {tf: {"direction": str, "valid": bool, ...}}

    Returns:
        int — score component (0 to SCORE_WEIGHT_TF_ALIGN)
    """
    if not tf_results:
        return 0

    total_valid  = 0
    agree_weight = 0.0

    for tf, data in tf_results.items():
        if not data.get("valid"):
            continue
        total_valid += 1
        direction = data.get("direction", DIR_SIDEWAYS)

        if direction == primary_direction:
            agree_weight += 1.0       # Full agreement
        elif direction == DIR_SIDEWAYS:
            agree_weight += 0.5       # Neutral — partial credit
        # Opposite direction: 0

    if total_valid == 0:
        return 0

    ratio = agree_weight / total_valid
    score = int(round(ratio * SCORE_WEIGHT_TF_ALIGN))
    logger.debug(
        f"[MTF] TF alignment | agree_weight={agree_weight:.1f} "
        f"/ {total_valid} | ratio={ratio:.2f} | score={score}"
    )
    return score


def _score_vwap_distance(primary_price: float, primary_vwap: float) -> int:
    """
    Score component 2: VWAP proximity on the primary timeframe (0 – SCORE_WEIGHT_VWAP_DIST pts).

    Logic:
        - The closer price is to VWAP the riskier — low conviction
        - A clear separation (price well above or below VWAP) = higher score
        - Distance is normalized against a reference (50 pts = ~2% move from VWAP)
        - Capped at SCORE_WEIGHT_VWAP_DIST

    Reference: 50 NIFTY points separation = full VWAP score
    """
    if primary_vwap is None or primary_price is None:
        return 0

    distance = abs(primary_price - primary_vwap)

    # 50 pt distance = full score (tuned for intraday NIFTY range)
    reference_distance = 50.0
    ratio = min(distance / reference_distance, 1.0)
    score = int(round(ratio * SCORE_WEIGHT_VWAP_DIST))

    logger.debug(
        f"[MTF] VWAP distance | price={primary_price:.2f} vwap={primary_vwap:.2f} "
        f"dist={distance:.2f} | score={score}"
    )
    return score


def _score_ema_alignment(primary_direction: str, tf_results: dict) -> int:
    """
    Score component 3: EMA9 alignment consistency (0 – SCORE_WEIGHT_EMA_ALIGN pts).

    Logic:
        - For each valid TF, check if EMA9 is on the same side of price as primary direction
        - BULLISH: price > EMA9 is aligned
        - BEARISH: price < EMA9 is aligned
        - Score scales with proportion of TFs where EMA9 agrees

    Args:
        primary_direction: DIR_BULLISH or DIR_BEARISH
        tf_results: dict of {tf: {"price": float, "ema9": float, "valid": bool}}

    Returns:
        int — score component (0 to SCORE_WEIGHT_EMA_ALIGN)
    """
    if not tf_results:
        return 0

    total_valid = 0
    aligned     = 0

    for tf, data in tf_results.items():
        if not data.get("valid"):
            continue
        price = data.get("price")
        ema9  = data.get("ema9")
        if price is None or ema9 is None:
            continue

        total_valid += 1

        if primary_direction == DIR_BULLISH and price > ema9:
            aligned += 1
        elif primary_direction == DIR_BEARISH and price < ema9:
            aligned += 1

    if total_valid == 0:
        return 0

    ratio = aligned / total_valid
    score = int(round(ratio * SCORE_WEIGHT_EMA_ALIGN))

    logger.debug(
        f"[MTF] EMA alignment | aligned={aligned}/{total_valid} "
        f"| ratio={ratio:.2f} | score={score}"
    )
    return score


def _classify_confidence(score: int) -> str:
    """
    Map raw score (0–100) to a named confidence level.

    HIGH   : score >= CONFIDENCE_HIGH_THRESHOLD  (default 70)
    MEDIUM : score >= CONFIDENCE_MED_THRESHOLD   (default 45)
    LOW    : score < CONFIDENCE_MED_THRESHOLD
    """
    if score >= CONFIDENCE_HIGH_THRESHOLD:
        return CONFIDENCE_HIGH
    elif score >= CONFIDENCE_MED_THRESHOLD:
        return CONFIDENCE_MEDIUM
    else:
        return CONFIDENCE_LOW


def _build_trade_signal(direction: str, price: float) -> dict:
    """
    Build signal dict matching the shape returned by decide_signal() in assistant.
    """
    strike = round(price / NIFTY_STRIKE_INTERVAL) * NIFTY_STRIKE_INTERVAL

    if direction == DIR_BULLISH:
        return {
            "trend":        "BULLISH / CE BIAS",
            "trade_signal": f"BUY {strike} CE",
            "stop_loss":    f"{STOP_LOSS_POINTS} Points",
            "target1":      f"{TARGET_1_POINTS} Points",
            "target2":      f"{TARGET_2_POINTS} Points",
            "target3":      f"{TARGET_3_POINTS} Points",
            "is_trade":     True,
        }
    elif direction == DIR_BEARISH:
        return {
            "trend":        "BEARISH / PE BIAS",
            "trade_signal": f"BUY {strike} PE",
            "stop_loss":    f"{STOP_LOSS_POINTS} Points",
            "target1":      f"{TARGET_1_POINTS} Points",
            "target2":      f"{TARGET_2_POINTS} Points",
            "target3":      f"{TARGET_3_POINTS} Points",
            "is_trade":     True,
        }
    else:
        return {
            "trend":        "SIDEWAYS",
            "trade_signal": "NO TRADE",
            "stop_loss":    "N/A",
            "target1":      "N/A",
            "target2":      "N/A",
            "target3":      "N/A",
            "is_trade":     False,
        }


# =========================
# MULTI-TIMEFRAME ANALYZER
# =========================

class MultiTimeframeAnalyzer:
    """
    Orchestrates multi-timeframe analysis for a single scan cycle.

    One instance shared by ai_trading_assistant per scan cycle.
    Playwright page object is passed in at call time (not stored at __init__)
    to avoid stale references across Playwright context restarts.

    Usage:
        analyzer = MultiTimeframeAnalyzer()
        result = analyzer.analyze(page, screenshot_dir, timestamp)

        if not result["valid"]:
            # primary TF OCR failed — skip cycle
            continue

        if result["confidence_level"] == "LOW":
            # reject trade — insufficient confirmation
            continue

        # Use result["trade_signal"], result["confidence_score"], etc.
    """

    def __init__(self) -> None:
        logger.info(
            f"[MTF] MultiTimeframeAnalyzer initialized | "
            f"timeframes={TIMEFRAMES} | primary={PRIMARY_TIMEFRAME} | "
            f"thresholds: HIGH>={CONFIDENCE_HIGH_THRESHOLD} MED>={CONFIDENCE_MED_THRESHOLD}"
        )

    # =========================
    # PRIMARY PUBLIC METHOD
    # =========================

    def analyze(self, page, screenshot_dir: Path, timestamp: str) -> dict:
        """
        Run the full multi-timeframe analysis pipeline.

        Steps:
            1. For each TF in TIMEFRAMES:
               a. Switch TradingView to that TF
               b. Wait TF_WAIT_MS for re-render
               c. Screenshot
               d. OCR extract (price, VWAP, EMA9)
               e. Record result
            2. Restore chart to PRIMARY_TIMEFRAME
            3. Compute direction per TF
            4. Anchor primary direction from PRIMARY_TIMEFRAME result
            5. Compute confidence score (3 components)
            6. Classify confidence level
            7. Build trade signal dict
            8. Return full MTFResult

        Args:
            page:           Playwright page object connected to TradingView
            screenshot_dir: Directory for this scan's screenshots (today's folder)
            timestamp:      Timestamp string "HH-MM-SS" for filename

        Returns:
            MTFResult dict (see module docstring for schema)
        """
        result = {
            "primary_direction": None,
            "trade_signal":      None,
            "is_trade":          False,
            "confidence_score":  0,
            "confidence_level":  CONFIDENCE_LOW,
            "timeframe_data":    {},
            "alignment_summary": "0/0",
            "aligned_count":     0,
            "total_tf_count":    len(TIMEFRAMES),
            "error":             None,
            "valid":             False,
        }

        tf_results: dict = {}

        # =========================
        # PRE-SCAN: DISMISS ADS / POPUPS
        # =========================
        # Run once before the loop to clear any overlay blocking the chart.
        _dismiss_ads(page)

        # =========================
        # STEP 1: PER-TIMEFRAME EXTRACTION
        # =========================

        for tf in TIMEFRAMES:
            logger.info(f"[MTF] Processing timeframe: {tf}")

            # --- Dismiss any ad that appeared since last TF switch ---
            _dismiss_ads(page)

            # --- Switch TradingView chart to this TF ---
            switched = _switch_timeframe(page, tf)
            if not switched:
                logger.warning(f"[MTF] Could not switch to {tf} — using screenshot as-is")

            # --- Wait for chart re-render ---
            page.wait_for_timeout(TF_WAIT_MS)

            # --- Dismiss any ad triggered by the TF switch ---
            _dismiss_ads(page)

            # --- Screenshot ---
            shot_path = screenshot_dir / f"mtf_{tf}_{timestamp}.png"
            try:
                page.screenshot(path=str(shot_path))
                logger.debug(f"[MTF] Screenshot saved: {shot_path.name}")
            except Exception as e:
                logger.error(f"[MTF] Screenshot failed for {tf}: {e}")
                tf_results[tf] = {"valid": False, "error": str(e)}
                continue

            # --- OCR ---
            ocr = extract_market_values(shot_path)

            if not ocr["valid"]:
                logger.warning(
                    f"[MTF] OCR failed for {tf}: {ocr['error']}"
                )
                tf_results[tf] = {
                    "valid":     False,
                    "error":     ocr["error"],
                    "price":     None,
                    "vwap":      None,
                    "ema9":      None,
                    "direction": None,
                }
                continue

            price = ocr["current_price"]
            vwap  = ocr["vwap"]
            ema9  = ocr["ema9"]

            # --- DOM Fallback: read VWAP / EMA9 from TradingView page text ---
            # OCR frequently garbles the small indicator legend text.
            # Playwright reads the page's HTML directly — no image processing.
            if vwap is None or ema9 is None:
                dom = _extract_indicators_from_dom(page)
                if vwap is None and dom["vwap"] is not None:
                    vwap = dom["vwap"]
                    logger.info("[MTF] VWAP from DOM fallback: %.2f", vwap)
                if ema9 is None and dom["ema9"] is not None:
                    ema9 = dom["ema9"]
                    logger.info("[MTF] EMA9 from DOM fallback: %.2f", ema9)

            direction = _tf_direction(price, vwap, ema9)

            tf_results[tf] = {
                "valid":     True,
                "price":     price,
                "vwap":      vwap,
                "ema9":      ema9,
                "direction": direction,
                "error":     None,
            }

            logger.info(
                "[MTF] %s | price=%.2f vwap=%s ema9=%s | direction=%s",
                tf, price,
                ("%.2f" % vwap) if vwap else "None",
                ("%.2f" % ema9) if ema9 else "None",
                direction,
            )

        result["timeframe_data"] = tf_results

        # =========================
        # STEP 2: RESTORE PRIMARY TIMEFRAME
        # =========================

        logger.info(f"[MTF] Restoring chart to primary TF: {PRIMARY_TIMEFRAME}")
        _switch_timeframe(page, PRIMARY_TIMEFRAME)
        page.wait_for_timeout(TF_WAIT_MS)

        # =========================
        # STEP 3: ANCHOR PRIMARY DIRECTION
        # =========================

        primary_data = tf_results.get(PRIMARY_TIMEFRAME, {})

        if not primary_data.get("valid"):
            result["error"] = (
                f"Primary timeframe ({PRIMARY_TIMEFRAME}) OCR failed — "
                f"cannot determine trade direction"
            )
            logger.error("[MTF] %s", result["error"])
            return result

        primary_direction = primary_data["direction"]
        primary_price     = primary_data["price"]
        primary_vwap      = primary_data["vwap"]
        primary_ema9      = primary_data["ema9"]

        result["primary_direction"] = primary_direction
        result["valid"] = True

        # =========================
        # STEP 4: ALIGNMENT COUNT
        # =========================

        aligned_count = sum(
            1 for tf, data in tf_results.items()
            if data.get("valid") and data.get("direction") == primary_direction
        )
        total_valid = sum(1 for d in tf_results.values() if d.get("valid"))

        result["aligned_count"]     = aligned_count
        result["total_tf_count"]    = total_valid
        result["alignment_summary"] = f"{aligned_count}/{total_valid} {primary_direction}"

        logger.info(
            "[MTF] Alignment: %s | primary=%s | price=%.2f",
            result["alignment_summary"], primary_direction,
            primary_price if primary_price else 0.0,
        )

        # =========================
        # STEP 5: CONFIDENCE SCORE
        # =========================

        score = 0

        # Component 1: Timeframe alignment (50 pts max)
        if total_valid > 0:
            align_ratio = aligned_count / total_valid
            score += int(align_ratio * SCORE_WEIGHT_TF_ALIGN)

        # Component 2: VWAP proximity (25 pts max)
        # Closer price is to VWAP, higher conviction
        if primary_vwap and primary_price:
            vwap_dist = abs(primary_price - primary_vwap)
            if vwap_dist <= 10:
                score += SCORE_WEIGHT_VWAP_DIST        # Full score: very close
            elif vwap_dist <= 30:
                score += int(SCORE_WEIGHT_VWAP_DIST * 0.75)
            elif vwap_dist <= 60:
                score += int(SCORE_WEIGHT_VWAP_DIST * 0.5)
            elif vwap_dist <= 100:
                score += int(SCORE_WEIGHT_VWAP_DIST * 0.25)
            # > 100 pts: 0 score — too far from VWAP

        # Component 3: EMA9 consistency (25 pts max)
        # All valid TFs where EMA9 agrees with primary direction
        if primary_ema9 and primary_price:
            ema_agrees = 0
            ema_total  = 0
            for tf, data in tf_results.items():
                if not data.get("valid") or data.get("ema9") is None:
                    continue
                ema_total += 1
                tf_ema    = data["ema9"]
                tf_price  = data["price"]
                if primary_direction == DIR_BULLISH and tf_price > tf_ema:
                    ema_agrees += 1
                elif primary_direction == DIR_BEARISH and tf_price < tf_ema:
                    ema_agrees += 1
            if ema_total > 0:
                score += int((ema_agrees / ema_total) * SCORE_WEIGHT_EMA_ALIGN)

        score = min(100, max(0, score))

        if score >= CONFIDENCE_HIGH_THRESHOLD:
            confidence_level = CONFIDENCE_HIGH
        elif score >= CONFIDENCE_MED_THRESHOLD:
            confidence_level = CONFIDENCE_MEDIUM
        else:
            confidence_level = CONFIDENCE_LOW

        result["confidence_score"] = score
        result["confidence_level"] = confidence_level

        logger.info(
            "[MTF] Confidence: %d/100 (%s) | components: align=%.0f%% vwap_dist=%s ema_agree=%s",
            score, confidence_level,
            (aligned_count / total_valid * 100) if total_valid else 0,
            ("%.1f pts" % abs(primary_price - primary_vwap)) if primary_vwap and primary_price else "N/A",
            ("yes" if primary_ema9 and primary_price and
             ((primary_direction == DIR_BULLISH and primary_price > primary_ema9) or
              (primary_direction == DIR_BEARISH and primary_price < primary_ema9))
             else "no") if primary_ema9 else "N/A",
        )

        # =========================
        # STEP 6: TRADE SIGNAL
        # =========================

        is_trade = (
            primary_direction in (DIR_BULLISH, DIR_BEARISH)
            and confidence_level in (CONFIDENCE_HIGH, CONFIDENCE_MEDIUM)
        )

        if is_trade:
            sig_dict = _build_trade_signal(primary_direction, primary_price)
            result.update(sig_dict)
            logger.info(
                "[MTF] Trade signal: %s | SL=%s | T1=%s T2=%s T3=%s",
                result["trade_signal"], result["stop_loss"],
                result["target1"], result["target2"], result["target3"],
            )
        else:
            sig_dict = _build_trade_signal("SIDEWAYS", primary_price)
            result.update(sig_dict)
            logger.info(
                "[MTF] No trade: direction=%s confidence=%s (%d/100)",
                primary_direction, confidence_level, score,
            )

        return result
