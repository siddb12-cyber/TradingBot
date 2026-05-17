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

def _tf_direction(price: float, vwap: float, ema9: float) -> str:
    """
    Determine bullish / bearish / sideways for a single timeframe.
    Matches the same logic used in ai_trading_assistant.decide_signal().
    """
    if price > vwap and price > ema9:
        return DIR_BULLISH
    elif price < vwap and price < ema9:
        return DIR_BEARISH
    else:
        return DIR_SIDEWAYS


def _switch_timeframe(page, tf_label: str) -> bool:
    """
    Click the TradingView timeframe toolbar button for the given label.

    TradingView renders timeframe buttons in a horizontal toolbar at the top
    of the chart. The button text matches TF_SELECTOR_MAP values.

    Strategy (in order):
        1. Try page.get_by_role("button", name=label, exact=True) — cleanest
        2. Try page.locator(f'[data-value="{label}"]') — TradingView data attribute
        3. Try pressing keyboard shortcut via page.keyboard.press() — fallback

    Returns True if click succeeded, False otherwise.
    """
    selector_label = TF_SELECTOR_MAP.get(tf_label, tf_label)

    # --- Attempt 1: role-based button click ---
    try:
        btn = page.get_by_role("button", name=selector_label, exact=True)
        if btn.count() > 0:
            btn.first.click()
            logger.debug(f"[MTF] Switched to {tf_label} via role button (label={selector_label})")
            return True
    except Exception as e:
        logger.debug(f"[MTF] Role button click failed for {tf_label}: {e}")

    # --- Attempt 2: data-value attribute selector ---
    try:
        locator = page.locator(f'[data-value="{selector_label}"]')
        if locator.count() > 0:
            locator.first.click()
            logger.debug(f"[MTF] Switched to {tf_label} via data-value selector")
            return True
    except Exception as e:
        logger.debug(f"[MTF] data-value click failed for {tf_label}: {e}")

    # --- Attempt 3: keyboard shortcut (TradingView standard: Alt+number or direct) ---
    # TradingView shortcuts for timeframes (works when chart is focused):
    # These vary by TradingView version — using keyboard as last resort only
    shortcut_map = {
        "5m":  "5",
        "15m": "5",   # no direct key for 15m — handled by data-value above
        "1h":  "6",   # TradingView key "6" = 1H in some versions
    }
    shortcut = shortcut_map.get(tf_label)
    if shortcut:
        try:
            page.keyboard.press(shortcut)
            logger.debug(f"[MTF] Switched to {tf_label} via keyboard shortcut: {shortcut}")
            return True
        except Exception as e:
            logger.debug(f"[MTF] Keyboard shortcut failed for {tf_label}: {e}")

    logger.warning(f"[MTF] All switch methods failed for timeframe: {tf_label}")
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
        # STEP 1: PER-TIMEFRAME EXTRACTION
        # =========================

        for tf in TIMEFRAMES:
            logger.info(f"[MTF] Processing timeframe: {tf}")

            # --- Switch TradingView chart to this TF ---
            switched = _switch_timeframe(page, tf)
            if not switched:
                logger.warning(f"[MTF] Could not switch to {tf} — using screenshot as-is")

            # --- Wait for chart re-render ---
            page.wait_for_timeout(TF_WAIT_MS)

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
                f"[MTF] {tf} | price={price:.2f} vwap={vwap:.2f} ema9={ema9:.2f} "
                f"| direction={direction}"
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
            result["valid"] = False
            logger.error(f"[MTF] {result['error']}")
            return result

        primary_direction = primary_data["direction"]
        primary_price     = primary_data["price"]
        primary_vwap      = primary_data["vwap"]

        result["primary_direction"] = primary_direction
        result["valid"]             = True

        # =========================
        # STEP 4: CONFIDENCE SCORING
        # =========================

        # Component 1: Timeframe alignment (50 pts)
        score_tf = _score_tf_alignment(primary_direction, tf_results)

        # Component 2: VWAP distance on primary TF (25 pts)
        score_vwap = _score_vwap_distance(primary_price, primary_vwap)

        # Component 3: EMA9 alignment across TFs (25 pts)
        score_ema = _score_ema_alignment(primary_direction, tf_results)

        # Total score (0–100)
        total_score = score_tf + score_vwap + score_ema
        total_score = max(0, min(100, total_score))   # hard clamp

        confidence_level = _classify_confidence(total_score)

        result["confidence_score"] = total_score
        result["confidence_level"] = confidence_level

        logger.info(
            f"[MTF] Confidence | tf_align={score_tf} + vwap_dist={score_vwap} "
            f"+ ema_align={score_ema} = {total_score} → {confidence_level}"
        )

        # =========================
        # STEP 5: ALIGNMENT SUMMARY
        # =========================

        # Count TFs agreeing with primary direction (excluding sideways)
        aligned_count = 0
        valid_count   = 0

        for tf, data in tf_results.items():
            if not data.get("valid"):
                continue
            valid_count += 1
            if data.get("direction") == primary_direction:
                aligned_count += 1

        result["aligned_count"]     = aligned_count
        result["total_tf_count"]    = valid_count
        result["alignment_summary"] = f"{aligned_count}/{valid_count} {primary_direction}"

        logger.info(f"[MTF] Alignment: {result['alignment_summary']}")

        # =========================
        # STEP 6: BUILD TRADE SIGNAL
        # =========================

        # If primary is sideways OR confidence is LOW → no trade
        if primary_direction == DIR_SIDEWAYS or confidence_level == CONFIDENCE_LOW:
            sig = _build_trade_signal(DIR_SIDEWAYS, primary_price)
        else:
            sig = _build_trade_signal(primary_direction, primary_price)

        # Attach MTF metadata to signal dict for Telegram builder
        sig["confidence_score"] = total_score
        sig["confidence_level"] = confidence_level
        sig["alignment_summary"] = result["alignment_summary"]
        sig["timeframe_data"]    = tf_results

        result["trade_signal"] = sig["trade_signal"]
        result["is_trade"]     = sig["is_trade"]

        # Merge signal into result for easy access by caller
        result.update({k: v for k, v in sig.items() if k not in result})

        # =========================
        # DONE
        # =========================

        logger.info(
            f"[MTF] Analysis complete | direction={primary_direction} | "
            f"signal={sig['trade_signal']} | score={total_score} | level={confidence_level}"
        )

        return result

    # =========================
    # REPR
    # =========================

    def __repr__(self) -> str:
        return (
            f"MultiTimeframeAnalyzer("
            f"timeframes={TIMEFRAMES}, "
            f"primary={PRIMARY_TIMEFRAME})"
        )
