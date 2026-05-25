"""
core/signal_engine.py
=====================
API-based signal computation engine for TradingBot.

Replaces the OCR-dependent MultiTimeframeAnalyzer (multi_timeframe.py) with a
clean API-driven pipeline:

  DataEngine.get_analysis()       → raw MTF data (price, VWAP, EMA9 per TF)
      ↓
  SignalEngine.compute()          → confidence scoring + OI + sentiment
      ↓
  SignalResult                    → structured signal dict for trading_engine.py

Confidence scoring (0–100, identical weights to the old OCR system):
  - TF alignment   : 50 pts  (how many TFs agree with primary direction)
  - VWAP distance  : 25 pts  (price separation from VWAP = conviction)
  - EMA9 alignment : 25 pts  (EMA9 agrees with direction across TFs)

OI and sentiment adjustments are applied on top of the base score using the
existing OIAnalysis and NewsSentimentEngine modules unchanged.

Output (SignalResult dict) is compatible with DecisionLogger.log() and
trading_engine.py's approval flow.

Paper Trading Only — no execution happens here.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, Optional

from config.config import (
    # Strategy constants
    NIFTY_STRIKE_INTERVAL,
    STOP_LOSS_POINTS,
    TARGET_1_POINTS,
    TARGET_2_POINTS,
    TARGET_3_POINTS,
    # Confidence thresholds
    CONFIDENCE_VERY_HIGH_THRESHOLD,
    CONFIDENCE_HIGH_THRESHOLD,
    CONFIDENCE_MED_THRESHOLD,
    # Scoring weights
    SCORE_WEIGHT_TF_ALIGN,
    SCORE_WEIGHT_VWAP_DIST,
    SCORE_WEIGHT_EMA_ALIGN,
    # Scale-up config
    SCALE_UP_MULTIPLIER,
    SCALE_UP_MAX_LOTS,
    # Timeframes
    TIMEFRAMES,
    PRIMARY_TIMEFRAME,
)
from core.data_engine import DataEngine
from core.oi_analysis import OIAnalysis
from core.news_sentiment import NewsSentimentEngine

# =========================
# MODULE LOGGER
# =========================

logger = logging.getLogger(__name__)

# =========================
# DIRECTION CONSTANTS
# =========================

DIR_BULLISH  = "BULLISH"
DIR_BEARISH  = "BEARISH"
DIR_SIDEWAYS = "SIDEWAYS"

# Named confidence levels
CONFIDENCE_VERY_HIGH = "VERY HIGH"
CONFIDENCE_HIGH      = "HIGH"
CONFIDENCE_MEDIUM    = "MEDIUM"
CONFIDENCE_LOW       = "LOW"


# =========================
# CONFIDENCE SCORING
# (functions extracted from multi_timeframe.py — identical logic, no OCR dependency)
# =========================

def _score_tf_alignment(primary_dir: str, directions: Dict[str, str]) -> int:
    """
    Score component 1: Timeframe alignment (0 – SCORE_WEIGHT_TF_ALIGN pts).

    Each valid (non-SIDEWAYS) timeframe gets a weight proportional to its
    significance. Primary TF counts double to ensure it drives the signal.

    Primary TF     : weight 2
    Secondary TFs  : weight 1 each

    Score = (aligned_weight / total_valid_weight) * SCORE_WEIGHT_TF_ALIGN
    """
    if primary_dir in (DIR_SIDEWAYS, "N/A"):
        return 0

    agree_weight = 0.0
    total_weight = 0.0

    for tf, direction in directions.items():
        if direction in ("N/A", DIR_SIDEWAYS):
            continue

        # Primary TF is twice as important
        weight = 2.0 if tf == PRIMARY_TIMEFRAME else 1.0
        total_weight += weight

        if direction == primary_dir:
            agree_weight += weight

    if total_weight == 0:
        return 0

    ratio = agree_weight / total_weight
    score = int(round(ratio * SCORE_WEIGHT_TF_ALIGN))

    logger.debug(
        "[SignalEngine] TF alignment | agree_weight=%.1f / %.1f | ratio=%.2f | score=%d",
        agree_weight, total_weight, ratio, score,
    )
    return score


def _score_vwap_distance(price: Optional[float], vwap: Optional[float]) -> int:
    """
    Score component 2: VWAP proximity on the primary timeframe (0 – SCORE_WEIGHT_VWAP_DIST pts).

    The further price is from VWAP, the stronger the trend conviction.
    Reference: 50 NIFTY-point separation = full score.
    """
    if price is None or vwap is None:
        return 0

    distance  = abs(price - vwap)
    reference = 50.0   # 50 pts clear of VWAP = full conviction
    ratio     = min(distance / reference, 1.0)
    score     = int(round(ratio * SCORE_WEIGHT_VWAP_DIST))

    logger.debug(
        "[SignalEngine] VWAP dist | price=%.2f vwap=%.2f dist=%.2f | score=%d",
        price, vwap, distance, score,
    )
    return score


def _score_ema_alignment(primary_dir: str, tf_data: Dict[str, Dict]) -> int:
    """
    Score component 3: EMA9 directional agreement (0 – SCORE_WEIGHT_EMA_ALIGN pts).

    For each TF, check whether price is on the correct side of EMA9
    relative to the primary direction.
      BULLISH: price > ema9 is aligned
      BEARISH: price < ema9 is aligned

    Score scales with proportion of TFs that agree.
    """
    if primary_dir in (DIR_SIDEWAYS, "N/A"):
        return 0

    total_valid = 0
    aligned     = 0

    for tf, data in tf_data.items():
        price = data.get("price")
        ema9  = data.get("ema9")
        if price is None or ema9 is None:
            continue

        total_valid += 1

        if primary_dir == DIR_BULLISH and price > ema9:
            aligned += 1
        elif primary_dir == DIR_BEARISH and price < ema9:
            aligned += 1

    if total_valid == 0:
        return 0

    ratio = aligned / total_valid
    score = int(round(ratio * SCORE_WEIGHT_EMA_ALIGN))

    logger.debug(
        "[SignalEngine] EMA align | aligned=%d/%d | ratio=%.2f | score=%d",
        aligned, total_valid, ratio, score,
    )
    return score


def _classify_confidence(score: int) -> str:
    """
    Map raw score (0–100) to named confidence level.

    VERY HIGH : score >= CONFIDENCE_VERY_HIGH_THRESHOLD (85)
    HIGH      : score >= CONFIDENCE_HIGH_THRESHOLD      (70)
    MEDIUM    : score >= CONFIDENCE_MED_THRESHOLD       (45)
    LOW       : score < CONFIDENCE_MED_THRESHOLD
    """
    if score >= CONFIDENCE_VERY_HIGH_THRESHOLD:
        return CONFIDENCE_VERY_HIGH
    elif score >= CONFIDENCE_HIGH_THRESHOLD:
        return CONFIDENCE_HIGH
    elif score >= CONFIDENCE_MED_THRESHOLD:
        return CONFIDENCE_MEDIUM
    else:
        return CONFIDENCE_LOW


def _build_trade_signal(direction: str, price: float) -> Dict:
    """
    Build a trade signal dict from direction and current price.
    Identical format to multi_timeframe.py _build_trade_signal().
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
# SIGNAL ENGINE CLASS
# =========================

class SignalEngine:
    """
    Orchestrates signal generation using API data (no browser, no OCR).

    One shared instance in trading_engine.py — thread-safe because all
    mutable state lives in DataEngine, OIAnalysis, and NewsSentimentEngine.

    Usage
    -----
    engine = SignalEngine(data_engine)
    result = engine.compute()

    result is a dict with all fields needed by:
      - DecisionLogger.log()
      - TelegramApprovalBot.send_signal_request()
      - trading_engine.py decision gate

    Result schema
    -------------
    {
      # MTF raw data (from DataEngine — identical to MultiTimeframeAnalyzer.analyze())
      "valid"            : bool,
      "is_trade"         : bool,
      "direction"        : str,
      "alignment_count"  : int,
      "alignment_summary": str,
      "timeframe_data"   : dict,

      # Trade action (from _build_trade_signal)
      "trade_signal"     : str,   # e.g. "BUY 23800 CE"
      "trend"            : str,
      "stop_loss"        : str,
      "target1"          : str,
      "target2"          : str,
      "target3"          : str,

      # Confidence scoring
      "base_score"       : int,   # before OI/sentiment adjustments
      "adjusted_score"   : int,   # after OI/sentiment adjustments
      "confidence_level" : str,   # VERY HIGH / HIGH / MEDIUM / LOW

      # OI analysis result dict (from OIAnalysis.get_score_adjustment())
      "oi_result"        : dict,

      # Sentiment result dict (from NewsSentimentEngine.get_score_adjustment())
      "sent_result"      : dict,

      # Scale-up suggestion (only present when confidence = VERY HIGH)
      "scale_up"         : bool,
      "scale_up_lots"    : int,

      # Raw primary TF values (convenience shortcuts)
      "price"            : float | None,
      "vwap"             : float | None,
      "ema9"             : float | None,
    }
    """

    def __init__(self, data_engine: DataEngine) -> None:
        self._data    = data_engine
        self._oi      = OIAnalysis()
        self._sent    = NewsSentimentEngine()

        logger.info(
            "[SignalEngine] Initialised | TF weights: align=%d vwap=%d ema=%d | "
            "Thresholds: VH=%d H=%d M=%d",
            SCORE_WEIGHT_TF_ALIGN, SCORE_WEIGHT_VWAP_DIST, SCORE_WEIGHT_EMA_ALIGN,
            CONFIDENCE_VERY_HIGH_THRESHOLD, CONFIDENCE_HIGH_THRESHOLD, CONFIDENCE_MED_THRESHOLD,
        )

    # ------------------------------------------------------------------
    # PRIMARY PUBLIC METHOD
    # ------------------------------------------------------------------

    def compute(self) -> Dict:
        """
        Run one full signal computation cycle.

        Steps:
          1. Fetch multi-TF analysis from DataEngine
          2. Bail early if data is invalid or direction is SIDEWAYS
          3. Compute 3-component base confidence score
          4. Apply OI score adjustment
          5. Apply sentiment score adjustment
          6. Classify final confidence level
          7. Build and return full signal dict

        Returns
        -------
        dict — always returns a dict (never raises, never returns None)
        On invalid data: valid=False, is_trade=False
        """
        # ================================================================
        # STEP 1: Fetch MTF analysis from DataEngine
        # ================================================================
        mtf = self._data.get_analysis()

        if not mtf["valid"]:
            logger.warning("[SignalEngine] MTF data invalid — skipping signal computation")
            return self._empty_result(mtf, reason="Data fetch failed on one or more timeframes")

        # ================================================================
        # STEP 2: Bail if primary TF is SIDEWAYS
        # ================================================================
        direction = mtf["direction"]

        if direction == DIR_SIDEWAYS or not mtf["is_trade"]:
            logger.info("[SignalEngine] No trade — direction is SIDEWAYS")
            return self._empty_result(mtf, reason="Primary timeframe direction is SIDEWAYS")

        # ================================================================
        # STEP 3: Compute base confidence score (0–100)
        # ================================================================
        tf_data    = mtf.get("timeframe_data", {})
        directions = {tf: d.get("direction", DIR_SIDEWAYS) for tf, d in tf_data.items()}

        # Primary TF price and VWAP (for VWAP distance score)
        primary_data = tf_data.get(PRIMARY_TIMEFRAME, {})
        price        = primary_data.get("price")
        vwap         = primary_data.get("vwap")
        ema9         = primary_data.get("ema9")

        score_tf    = _score_tf_alignment(direction, directions)
        score_vwap  = _score_vwap_distance(price, vwap)
        score_ema   = _score_ema_alignment(direction, tf_data)
        base_score  = score_tf + score_vwap + score_ema
        base_score  = max(0, min(100, base_score))   # Clamp to [0, 100]

        logger.info(
            "[SignalEngine] Base score: %d (TF=%d + VWAP=%d + EMA=%d)",
            base_score, score_tf, score_vwap, score_ema,
        )

        # ================================================================
        # STEP 4: OI adjustment
        # ================================================================
        try:
            oi_result  = self._oi.get_score_adjustment(price or 0.0, direction)
            oi_adj     = oi_result.get("score_adjustment", 0)
        except Exception as exc:
            logger.warning("[SignalEngine] OI analysis failed: %s — skipping OI adjustment", exc)
            oi_result  = {}
            oi_adj     = 0

        # ================================================================
        # STEP 5: Sentiment adjustment
        # ================================================================
        try:
            sent_result = self._sent.get_score_adjustment(direction)
            sent_adj    = sent_result.get("total_adjustment", 0)
        except Exception as exc:
            logger.warning("[SignalEngine] Sentiment analysis failed: %s — skipping", exc)
            sent_result = {}
            sent_adj    = 0

        # ================================================================
        # STEP 6: Final adjusted score
        # ================================================================
        adjusted_score = base_score + oi_adj + sent_adj
        adjusted_score = max(0, min(100, adjusted_score))   # Clamp

        confidence_level = _classify_confidence(adjusted_score)

        logger.info(
            "[SignalEngine] Final score: %d (base=%d OI=%+d sent=%+d) → %s",
            adjusted_score, base_score, oi_adj, sent_adj, confidence_level,
        )

        # ================================================================
        # STEP 7: Build trade signal
        # ================================================================
        sig = _build_trade_signal(direction, price or 0)

        # ================================================================
        # STEP 8: Scale-up suggestion (VERY HIGH confidence only)
        # ================================================================
        scale_up      = confidence_level == CONFIDENCE_VERY_HIGH
        scale_up_lots = 0
        if scale_up:
            # Telegram suggestion only — no auto-execution
            standard_lots = 1
            scale_up_lots = min(
                int(standard_lots * SCALE_UP_MULTIPLIER),
                SCALE_UP_MAX_LOTS,
            )
            logger.info(
                "[SignalEngine] VERY HIGH confidence — scale-up suggestion: %d lots",
                scale_up_lots,
            )

        # ================================================================
        # ASSEMBLE FINAL RESULT
        # ================================================================
        result = {
            # MTF data (identical schema to MultiTimeframeAnalyzer.analyze())
            "valid":             mtf["valid"],
            "is_trade":          sig["is_trade"],
            "direction":         direction,
            "alignment_count":   mtf["alignment_count"],
            "alignment_summary": mtf["alignment_summary"],
            "timeframe_data":    tf_data,

            # Trade action
            "trade_signal":      sig["trade_signal"],
            "trend":             sig["trend"],
            "stop_loss":         sig["stop_loss"],
            "target1":           sig["target1"],
            "target2":           sig["target2"],
            "target3":           sig["target3"],

            # Confidence
            "base_score":        base_score,
            "adjusted_score":    adjusted_score,
            "confidence_level":  confidence_level,

            # OI and sentiment raw results (for DecisionLogger)
            "oi_result":         oi_result,
            "sent_result":       sent_result,

            # Scale-up
            "scale_up":          scale_up,
            "scale_up_lots":     scale_up_lots,

            # Convenience shortcuts
            "price":             price,
            "vwap":              vwap,
            "ema9":              ema9,
        }

        return result

    # ------------------------------------------------------------------
    # INTERNAL: EMPTY / INVALID RESULT BUILDER
    # ------------------------------------------------------------------

    def _empty_result(self, mtf: Dict, reason: str = "") -> Dict:
        """
        Build a no-trade result dict for invalid/sideways conditions.
        Maintains schema compatibility with a successful compute() call.
        """
        tf_data = mtf.get("timeframe_data", {})
        primary = tf_data.get(PRIMARY_TIMEFRAME, {})

        return {
            # MTF passthrough
            "valid":             mtf.get("valid", False),
            "is_trade":          False,
            "direction":         mtf.get("direction", DIR_SIDEWAYS),
            "alignment_count":   mtf.get("alignment_count", 0),
            "alignment_summary": mtf.get("alignment_summary", "N/A"),
            "timeframe_data":    tf_data,

            # No trade
            "trade_signal":      "NO TRADE",
            "trend":             "SIDEWAYS",
            "stop_loss":         "N/A",
            "target1":           "N/A",
            "target2":           "N/A",
            "target3":           "N/A",

            # Zeroed scores
            "base_score":        0,
            "adjusted_score":    0,
            "confidence_level":  CONFIDENCE_LOW,

            # Empty analysis results
            "oi_result":         {},
            "sent_result":       {},

            # No scale-up
            "scale_up":          False,
            "scale_up_lots":     0,

            # Price info if available
            "price":             primary.get("price"),
            "vwap":              primary.get("vwap"),
            "ema9":              primary.get("ema9"),

            # Reason for rejection (used by trading_engine.py in decision logs)
            "_reason":           reason,
        }
