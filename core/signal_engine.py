"""
core/signal_engine.py
=====================
Multi-layer signal computation engine for TradingBot.

Architecture: 6 scoring layers + hard filters + OI + sentiment adjustments.

─────────────────────────────────────────────────────────────────────────────
HARD FILTERS  (return NO TRADE regardless of score)
─────────────────────────────────────────────────────────────────────────────
  1. ADX < 18          → Sideways / choppy market — most losses happen here
  2. RSI > 75 (bull)   → Overbought — avoid entering at the top
  3. RSI < 25 (bear)   → Oversold  — avoid entering at the bottom
  4. ORB forming       → First 15 minutes — no confirmed range yet

─────────────────────────────────────────────────────────────────────────────
BASE SCORE  (0–100 points across 6 layers)
─────────────────────────────────────────────────────────────────────────────
  Layer 1: Trend Direction      25 pts  VWAP position + Supertrend
  Layer 2: TF Alignment         20 pts  5m / 15m / 1h agree
  Layer 3: Momentum             20 pts  RSI ideal zone + MACD histogram
  Layer 4: EMA Stack            15 pts  9/20/50 properly ordered
  Layer 5: Market Structure     10 pts  HH/HL (bull) or LH/LL (bear)
  Layer 6: Opening Range        10 pts  Price beyond ORB in signal direction
                                ──────
  Total base                   100 pts

─────────────────────────────────────────────────────────────────────────────
ADJUSTMENTS  (applied on top of base; clamped to 0–100 after OI/sentiment)
─────────────────────────────────────────────────────────────────────────────
  ADX strong (>30)              +8 pts
  ADX very strong (>40)         +12 pts  (replaces strong bonus)
  Candlestick pattern confirms  +5 pts
  RSI approaching extreme       -8 pts
  ADX borderline (18-22)        -8 pts
  OI adjustment                 ±15 pts  (from oi_analysis.py)
  Sentiment adjustment          ±20 pts  (from news_sentiment.py)

─────────────────────────────────────────────────────────────────────────────
CONFIDENCE THRESHOLDS
─────────────────────────────────────────────────────────────────────────────
  VERY HIGH : ≥ 85   (scale-up suggestion)
  HIGH      : ≥ 70
  MEDIUM    : ≥ 45   (minimum to trade — CONFIDENCE_MED_THRESHOLD)
  LOW       : < 45   (no trade)

─────────────────────────────────────────────────────────────────────────────
DYNAMIC STOP-LOSS
─────────────────────────────────────────────────────────────────────────────
  dynamic_sl_points = ATR(14) × ATR_SL_MULTIPLIER
  Clamped to [ATR_MIN_POINTS, ATR_MAX_POINTS]

Paper Trading Only — no execution happens here.
"""

import logging
from datetime import datetime
from typing import Dict, Optional

from config.settings import (
    # Strategy
    NIFTY_STRIKE_INTERVAL,
    STOP_LOSS_POINTS,
    TARGET_1_POINTS,
    TARGET_2_POINTS,
    TARGET_3_POINTS,
    # Confidence thresholds
    CONFIDENCE_VERY_HIGH_THRESHOLD,
    CONFIDENCE_HIGH_THRESHOLD,
    CONFIDENCE_MED_THRESHOLD,
    # Scale-up
    SCALE_UP_MULTIPLIER,
    SCALE_UP_MAX_LOTS,
    # Timeframes
    TIMEFRAMES,
    PRIMARY_TIMEFRAME,
    # Market open
    MARKET_OPEN_HOUR,
    MARKET_OPEN_MINUTE,
    # New indicator thresholds
    RSI_BULL_IDEAL_MIN, RSI_BULL_IDEAL_MAX,
    RSI_BEAR_IDEAL_MIN, RSI_BEAR_IDEAL_MAX,
    RSI_OVERBOUGHT_BLOCK, RSI_OVERSOLD_BLOCK,
    RSI_WARN_UPPER, RSI_WARN_LOWER,
    ADX_SIDEWAYS_BLOCK, ADX_TREND_THRESHOLD,
    ADX_STRONG_THRESHOLD, ADX_VERY_STRONG,
    ATR_SL_MULTIPLIER, ATR_MIN_POINTS, ATR_MAX_POINTS,
    ORB_WINDOW_SKIP, ORB_MINUTES,
    # Scoring weights
    SCORE_WEIGHT_TREND,
    SCORE_WEIGHT_TF_ALIGN,
    SCORE_WEIGHT_MOMENTUM,
    SCORE_WEIGHT_EMA_STACK,
    SCORE_WEIGHT_MARKET_STRUCTURE,
    SCORE_WEIGHT_ORB,
    # Bonuses / penalties
    SCORE_BONUS_ADX_STRONG,
    SCORE_BONUS_ADX_VERY_STRONG,
    SCORE_BONUS_CANDLE_PATTERN,
    SCORE_PENALTY_ADX_BORDERLINE,
    SCORE_PENALTY_RSI_WARN,
    SCORE_PENALTY_LUNCH_HOUR,
    LUNCH_AVOID_ENABLED,
    LUNCH_AVOID_START_HOUR, LUNCH_AVOID_START_MINUTE,
    LUNCH_AVOID_END_HOUR,   LUNCH_AVOID_END_MINUTE,
)
from core.data_engine import DataEngine
from core.oi_analysis import OIAnalysis
from core.news_sentiment import NewsSentimentEngine

logger = logging.getLogger(__name__)

# =============================================================================
# DIRECTION CONSTANTS
# =============================================================================

DIR_BULLISH  = "BULLISH"
DIR_BEARISH  = "BEARISH"
DIR_SIDEWAYS = "SIDEWAYS"

CONFIDENCE_VERY_HIGH = "VERY HIGH"
CONFIDENCE_HIGH      = "HIGH"
CONFIDENCE_MEDIUM    = "MEDIUM"
CONFIDENCE_LOW       = "LOW"


# =============================================================================
# HELPERS
# =============================================================================

def _safe(val, default=0.0):
    """Return val if not None/NaN, else default."""
    if val is None:
        return default
    try:
        import math
        if math.isnan(float(val)):
            return default
    except (TypeError, ValueError):
        return default
    return val


def _classify_confidence(score: int) -> str:
    if score >= CONFIDENCE_VERY_HIGH_THRESHOLD:
        return CONFIDENCE_VERY_HIGH
    if score >= CONFIDENCE_HIGH_THRESHOLD:
        return CONFIDENCE_HIGH
    if score >= CONFIDENCE_MED_THRESHOLD:
        return CONFIDENCE_MEDIUM
    return CONFIDENCE_LOW


def _build_trade_signal(direction: str, price: float, sl_points: float) -> Dict:
    """
    Build the trade action dict from direction + price.
    Uses dynamic SL if provided, falls back to STOP_LOSS_POINTS.
    """
    strike = round(price / NIFTY_STRIKE_INTERVAL) * NIFTY_STRIKE_INTERVAL
    sl     = int(round(sl_points))

    if direction == DIR_BULLISH:
        return {
            "trend":        "BULLISH / CE BIAS",
            "trade_signal": f"BUY {strike} CE",
            "stop_loss":    f"{sl} Points (ATR-based)",
            "target1":      f"{TARGET_1_POINTS} Points",
            "target2":      f"{TARGET_2_POINTS} Points",
            "target3":      f"{TARGET_3_POINTS} Points",
            "is_trade":     True,
        }
    if direction == DIR_BEARISH:
        return {
            "trend":        "BEARISH / PE BIAS",
            "trade_signal": f"BUY {strike} PE",
            "stop_loss":    f"{sl} Points (ATR-based)",
            "target1":      f"{TARGET_1_POINTS} Points",
            "target2":      f"{TARGET_2_POINTS} Points",
            "target3":      f"{TARGET_3_POINTS} Points",
            "is_trade":     True,
        }
    return {
        "trend": "SIDEWAYS", "trade_signal": "NO TRADE",
        "stop_loss": "N/A", "target1": "N/A",
        "target2": "N/A", "target3": "N/A",
        "is_trade": False,
    }


# =============================================================================
# SIGNAL ENGINE
# =============================================================================

class SignalEngine:
    """
    6-layer multi-indicator signal engine.

    Replaces the old 3-component VWAP+EMA+TF engine.
    Adds: ADX hard filter, RSI hard filter, MACD, Supertrend,
          EMA stack (9/20/50), Opening Range, Market Structure,
          Candlestick patterns, ATR dynamic SL.

    Usage (unchanged from engine.py perspective)
    ----
        engine = SignalEngine(data_engine)
        result = engine.compute()
    """

    def __init__(self, data_engine: DataEngine) -> None:
        self._data = data_engine
        self._oi   = OIAnalysis()
        self._sent = NewsSentimentEngine()
        logger.info(
            "[SignalEngine] Initialised — 6-layer scoring | "
            "Layers: Trend=%d TF=%d Mom=%d EMA=%d MS=%d ORB=%d | "
            "Hard filters: ADX<%.0f RSI>%.0f RSI<%.0f",
            SCORE_WEIGHT_TREND, SCORE_WEIGHT_TF_ALIGN,
            SCORE_WEIGHT_MOMENTUM, SCORE_WEIGHT_EMA_STACK,
            SCORE_WEIGHT_MARKET_STRUCTURE, SCORE_WEIGHT_ORB,
            ADX_SIDEWAYS_BLOCK, RSI_OVERBOUGHT_BLOCK, RSI_OVERSOLD_BLOCK,
        )

    # =========================================================================
    # PRIMARY PUBLIC METHOD
    # =========================================================================

    def compute(self) -> Dict:
        """
        Run one full signal computation cycle.

        Returns
        -------
        dict — always returns a dict (never raises, never returns None)

        Result schema (adds these to the existing schema)
        --------------------------------------------------
        dynamic_sl_points   float   — ATR-based stop-loss in NIFTY points
        score_breakdown     dict    — per-layer scores for transparency
        indicators          dict    — raw indicator values from primary TF
        hard_filtered       bool    — True if a hard filter blocked the trade
        hard_filter_reason  str     — human-readable block reason
        market_structure    str     — BULLISH / BEARISH / SIDEWAYS
        candle_pattern      dict    — {pattern, bias, strength}
        orb                 dict    — {orb_high, orb_low, orb_range, valid}
        """
        # =================================================================
        # STEP 1: Fetch MTF analysis
        # =================================================================
        mtf = self._data.get_analysis()

        if not mtf["valid"]:
            logger.warning("[SignalEngine] MTF data invalid — skipping")
            return self._empty_result(mtf, reason="Data fetch failed on one or more timeframes")

        # =================================================================
        # STEP 2: Quick bail on primary-TF SIDEWAYS
        # =================================================================
        direction = mtf["direction"]
        if direction == DIR_SIDEWAYS or not mtf["is_trade"]:
            return self._empty_result(mtf, reason="Primary timeframe direction is SIDEWAYS")

        # =================================================================
        # STEP 3: Pull primary-TF indicator values
        # =================================================================
        tf_data  = mtf.get("timeframe_data", {})
        primary  = tf_data.get(PRIMARY_TIMEFRAME, {})
        orb      = mtf.get("orb", {})
        ms       = mtf.get("market_structure", DIR_SIDEWAYS)
        candle   = mtf.get("candle_pattern", {"pattern": "NONE", "bias": "NEUTRAL", "strength": 0})

        price    = _safe(primary.get("price"),    0.0)
        vwap     = _safe(primary.get("vwap"),     price)
        ema9     = _safe(primary.get("ema9"),     price)
        ema20    = _safe(primary.get("ema20"),    price)
        ema50    = _safe(primary.get("ema50"),    price)
        rsi      = _safe(primary.get("rsi"),      50.0)
        adx      = _safe(primary.get("adx"),      0.0)
        atr      = _safe(primary.get("atr"),      0.0)
        macd_h   = _safe(primary.get("macd_hist"), 0.0)
        st_dir   = int(_safe(primary.get("st_dir"), 0))

        # =================================================================
        # STEP 4: HARD FILTERS — return empty result if blocked
        # =================================================================
        now = datetime.now()

        # Filter 1: ADX too low → choppy market
        if adx > 0 and adx < ADX_SIDEWAYS_BLOCK:
            reason = (
                f"ADX={adx:.1f} < {ADX_SIDEWAYS_BLOCK} "
                f"(sideways/choppy — avoid trading)"
            )
            logger.info("[SignalEngine] HARD FILTER: %s", reason)
            return self._empty_result(mtf, reason=reason, hard_filtered=True)

        # Filter 2: RSI overbought on bullish signal
        if direction == DIR_BULLISH and rsi > RSI_OVERBOUGHT_BLOCK:
            reason = (
                f"RSI={rsi:.1f} > {RSI_OVERBOUGHT_BLOCK} "
                f"(overbought — skip bull entry)"
            )
            logger.info("[SignalEngine] HARD FILTER: %s", reason)
            return self._empty_result(mtf, reason=reason, hard_filtered=True)

        # Filter 3: RSI oversold on bearish signal
        if direction == DIR_BEARISH and rsi < RSI_OVERSOLD_BLOCK:
            reason = (
                f"RSI={rsi:.1f} < {RSI_OVERSOLD_BLOCK} "
                f"(oversold — skip bear entry)"
            )
            logger.info("[SignalEngine] HARD FILTER: %s", reason)
            return self._empty_result(mtf, reason=reason, hard_filtered=True)

        # Filter 4: ORB window (first 15 min — range not established)
        if ORB_WINDOW_SKIP:
            orb_end_min = MARKET_OPEN_MINUTE + ORB_MINUTES
            if now.hour == MARKET_OPEN_HOUR and now.minute < orb_end_min:
                reason = (
                    f"ORB forming ({MARKET_OPEN_HOUR}:{MARKET_OPEN_MINUTE:02d}"
                    f"–{MARKET_OPEN_HOUR}:{orb_end_min:02d}) — wait for range"
                )
                logger.info("[SignalEngine] HARD FILTER: %s", reason)
                return self._empty_result(mtf, reason=reason, hard_filtered=True)

        # =================================================================
        # STEP 5: 6-LAYER BASE SCORING
        # =================================================================

        s_trend  = self._score_trend_direction(direction, price, vwap, st_dir)
        s_tf     = self._score_tf_alignment(direction, tf_data)
        s_mom    = self._score_momentum(direction, rsi, macd_h)
        s_ema    = self._score_ema_stack(direction, price, ema9, ema20, ema50)
        s_ms     = self._score_market_structure(direction, ms)
        s_orb    = self._score_orb(direction, price, orb)

        breakdown = {
            "trend":            s_trend,
            "tf_alignment":     s_tf,
            "momentum":         s_mom,
            "ema_stack":        s_ema,
            "market_structure": s_ms,
            "orb":              s_orb,
        }
        base_score = sum(breakdown.values())

        # =================================================================
        # STEP 6: POST-SCORING ADJUSTMENTS (before OI/sentiment)
        # =================================================================

        # ADX bonus
        if adx >= ADX_VERY_STRONG:
            base_score += SCORE_BONUS_ADX_VERY_STRONG
            logger.debug("[SignalEngine] ADX very strong %.1f → +%d", adx, SCORE_BONUS_ADX_VERY_STRONG)
        elif adx >= ADX_STRONG_THRESHOLD:
            base_score += SCORE_BONUS_ADX_STRONG
            logger.debug("[SignalEngine] ADX strong %.1f → +%d", adx, SCORE_BONUS_ADX_STRONG)
        elif 0 < adx < ADX_TREND_THRESHOLD:
            base_score += SCORE_PENALTY_ADX_BORDERLINE
            logger.debug("[SignalEngine] ADX borderline %.1f → %d", adx, SCORE_PENALTY_ADX_BORDERLINE)

        # RSI warning penalty (approaching extreme but not blocked)
        if direction == DIR_BULLISH and rsi > RSI_WARN_UPPER:
            base_score += SCORE_PENALTY_RSI_WARN
            logger.debug("[SignalEngine] RSI %.1f approaching overbought → %d", rsi, SCORE_PENALTY_RSI_WARN)
        elif direction == DIR_BEARISH and rsi < RSI_WARN_LOWER:
            base_score += SCORE_PENALTY_RSI_WARN
            logger.debug("[SignalEngine] RSI %.1f approaching oversold → %d", rsi, SCORE_PENALTY_RSI_WARN)

        # Candlestick pattern bonus
        if candle["bias"] == direction and candle["strength"] >= 2:
            base_score += SCORE_BONUS_CANDLE_PATTERN
            logger.debug("[SignalEngine] Candle %s confirms %s → +%d",
                         candle["pattern"], direction, SCORE_BONUS_CANDLE_PATTERN)

        # Lunch hour penalty
        if LUNCH_AVOID_ENABLED:
            lunch_start = now.replace(hour=LUNCH_AVOID_START_HOUR, minute=LUNCH_AVOID_START_MINUTE)
            lunch_end   = now.replace(hour=LUNCH_AVOID_END_HOUR,   minute=LUNCH_AVOID_END_MINUTE)
            if lunch_start <= now < lunch_end:
                base_score += SCORE_PENALTY_LUNCH_HOUR
                logger.debug("[SignalEngine] Lunch hour penalty %d", SCORE_PENALTY_LUNCH_HOUR)

        base_score = max(0, min(100, base_score))

        logger.info(
            "[SignalEngine] Base score: %d | trend=%d tf=%d mom=%d ema=%d ms=%d orb=%d",
            base_score, s_trend, s_tf, s_mom, s_ema, s_ms, s_orb,
        )

        # =================================================================
        # STEP 7: OI adjustment
        # =================================================================
        try:
            oi_result = self._oi.get_score_adjustment(price, direction)
            oi_adj    = oi_result.get("score_adjustment", 0)
        except Exception as exc:
            logger.warning("[SignalEngine] OI failed: %s", exc)
            oi_result = {}
            oi_adj    = 0

        # =================================================================
        # STEP 8: Sentiment adjustment
        # =================================================================
        try:
            sent_result = self._sent.get_score_adjustment(direction)
            sent_adj    = sent_result.get("total_adjustment", 0)
        except Exception as exc:
            logger.warning("[SignalEngine] Sentiment failed: %s", exc)
            sent_result = {}
            sent_adj    = 0

        # =================================================================
        # STEP 9: Final adjusted score
        # =================================================================
        adjusted_score = max(0, min(100, base_score + oi_adj + sent_adj))
        confidence     = _classify_confidence(adjusted_score)

        logger.info(
            "[SignalEngine] Final: %d (base=%d OI=%+d sent=%+d) → %s",
            adjusted_score, base_score, oi_adj, sent_adj, confidence,
        )

        # =================================================================
        # STEP 10: ATR dynamic SL
        # =================================================================
        if atr > 0:
            dynamic_sl = round(atr * ATR_SL_MULTIPLIER, 1)
            dynamic_sl = max(ATR_MIN_POINTS, min(ATR_MAX_POINTS, dynamic_sl))
        else:
            dynamic_sl = float(STOP_LOSS_POINTS)

        # =================================================================
        # STEP 11: Build trade signal
        # =================================================================
        sig = _build_trade_signal(direction, price, dynamic_sl)

        # =================================================================
        # STEP 12: Scale-up suggestion
        # =================================================================
        scale_up      = (confidence == CONFIDENCE_VERY_HIGH)
        scale_up_lots = 0
        if scale_up:
            scale_up_lots = min(int(1 * SCALE_UP_MULTIPLIER), SCALE_UP_MAX_LOTS)
            logger.info("[SignalEngine] VERY HIGH confidence → scale-up %d lots", scale_up_lots)

        # =================================================================
        # ASSEMBLE RESULT
        # =================================================================
        directions_per_tf = {tf: d.get("direction", DIR_SIDEWAYS) for tf, d in tf_data.items()}
        aligned_count     = sum(1 for d in directions_per_tf.values() if d == direction)
        align_summary     = mtf.get("alignment_summary", "N/A")

        return {
            # MTF core (schema-compatible with old engine)
            "valid":             True,
            "is_trade":          sig["is_trade"],
            "direction":         direction,
            "alignment_count":   aligned_count,
            "alignment_summary": align_summary,
            "timeframe_data":    tf_data,

            # Trade action
            "trade_signal":      sig["trade_signal"],
            "trend":             sig["trend"],
            "stop_loss":         sig["stop_loss"],
            "target1":           sig["target1"],
            "target2":           sig["target2"],
            "target3":           sig["target3"],

            # Confidence scoring
            "base_score":        base_score,
            "adjusted_score":    adjusted_score,
            "confidence_level":  confidence,

            # OI + sentiment
            "oi_result":         oi_result,
            "sent_result":       sent_result,

            # Scale-up
            "scale_up":          scale_up,
            "scale_up_lots":     scale_up_lots,

            # Convenience shortcuts (primary TF)
            "price":             price,
            "vwap":              vwap,
            "ema9":              ema9,

            # ── NEW FIELDS ──────────────────────────────────────────────
            "dynamic_sl_points": dynamic_sl,
            "score_breakdown":   breakdown,
            "indicators": {
                "rsi":       round(rsi, 1),
                "adx":       round(adx, 1),
                "atr":       round(atr, 2),
                "macd_hist": round(macd_h, 3),
                "ema20":     round(ema20, 2),
                "ema50":     round(ema50, 2),
                "st_dir":    st_dir,
            },
            "hard_filtered":       False,
            "hard_filter_reason":  "",
            "market_structure":    ms,
            "candle_pattern":      candle,
            "orb":                 orb,
        }

    # =========================================================================
    # LAYER 1: TREND DIRECTION  (0–25 pts)
    # =========================================================================

    def _score_trend_direction(
        self,
        direction: str,
        price:    float,
        vwap:     float,
        st_dir:   int,
    ) -> int:
        """
        25 pts split across two sub-signals:
          VWAP position   : 12 pts  (price on correct side of VWAP)
          Supertrend dir  : 13 pts  (Supertrend confirms direction)
        """
        score = 0

        # VWAP sub-score (12 pts)
        if direction == DIR_BULLISH and price > vwap:
            score += 12
        elif direction == DIR_BEARISH and price < vwap:
            score += 12

        # Supertrend sub-score (13 pts)
        # st_dir: 1 = bullish, -1 = bearish, 0 = unknown
        if direction == DIR_BULLISH and st_dir == 1:
            score += 13
        elif direction == DIR_BEARISH and st_dir == -1:
            score += 13
        elif st_dir == 0:
            # Unknown / not computed — partial credit based on VWAP only
            score += 0   # No supertrend data, rely on VWAP only

        logger.debug("[SignalEngine] L1 Trend: %d/25 (VWAP %s | ST %+d)",
                     score,
                     "OK" if (direction == DIR_BULLISH and price > vwap) or
                             (direction == DIR_BEARISH and price < vwap) else "MISS",
                     st_dir)
        return score

    # =========================================================================
    # LAYER 2: TIMEFRAME ALIGNMENT  (0–20 pts)
    # =========================================================================

    def _score_tf_alignment(
        self,
        direction: str,
        tf_data:   Dict,
    ) -> int:
        """
        20 pts: How many timeframes agree with primary direction.

        3/3 aligned → 20 pts
        2/3 aligned → 12 pts
        1/3 aligned →  5 pts  (primary is the 1, others contradict)
        0/3 aligned →  0 pts  (shouldn't happen if direction != SIDEWAYS)
        """
        total_tfs  = len(TIMEFRAMES)
        aligned    = sum(
            1 for tf, d in tf_data.items()
            if d.get("direction") == direction
        )

        if total_tfs == 0:
            return 0

        ratio = aligned / total_tfs

        if ratio >= 1.0:
            score = SCORE_WEIGHT_TF_ALIGN           # 20
        elif ratio >= 0.65:
            score = int(SCORE_WEIGHT_TF_ALIGN * 0.6)  # 12
        else:
            score = int(SCORE_WEIGHT_TF_ALIGN * 0.25) # 5

        logger.debug("[SignalEngine] L2 TF align: %d/%d → %d/20",
                     aligned, total_tfs, score)
        return score

    # =========================================================================
    # LAYER 3: MOMENTUM  (0–20 pts)
    # =========================================================================

    def _score_momentum(
        self,
        direction: str,
        rsi:       float,
        macd_hist: float,
    ) -> int:
        """
        20 pts split:
          RSI in ideal zone  : 10 pts
          MACD histogram dir : 10 pts

        RSI zones for bull:
          52-70 → 10 pts (ideal)
          45-52 → 5 pts  (acceptable but weak)
          <45   → 0 pts  (below 50 = momentum not with bulls)

        RSI zones for bear:
          30-48 → 10 pts (ideal)
          48-55 → 5 pts  (acceptable but weak)
          >55   → 0 pts  (above 50 = momentum not with bears)
        """
        score = 0

        # RSI sub-score
        if direction == DIR_BULLISH:
            if RSI_BULL_IDEAL_MIN <= rsi <= RSI_BULL_IDEAL_MAX:
                score += 10
                logger.debug("[SignalEngine] RSI %.1f in bull ideal zone → +10", rsi)
            elif 45.0 <= rsi < RSI_BULL_IDEAL_MIN:
                score += 5
                logger.debug("[SignalEngine] RSI %.1f acceptable bull zone → +5", rsi)
        elif direction == DIR_BEARISH:
            if RSI_BEAR_IDEAL_MIN <= rsi <= RSI_BEAR_IDEAL_MAX:
                score += 10
                logger.debug("[SignalEngine] RSI %.1f in bear ideal zone → +10", rsi)
            elif RSI_BEAR_IDEAL_MAX < rsi <= 55.0:
                score += 5
                logger.debug("[SignalEngine] RSI %.1f acceptable bear zone → +5", rsi)

        # MACD histogram sub-score
        if direction == DIR_BULLISH and macd_hist > 0:
            score += 10
            logger.debug("[SignalEngine] MACD hist +%.3f confirms bull → +10", macd_hist)
        elif direction == DIR_BEARISH and macd_hist < 0:
            score += 10
            logger.debug("[SignalEngine] MACD hist %.3f confirms bear → +10", macd_hist)
        elif macd_hist == 0.0:
            # Unknown / not computed
            score += 5    # Neutral — give half credit
            logger.debug("[SignalEngine] MACD hist unknown → +5 (neutral)")

        logger.debug("[SignalEngine] L3 Momentum: %d/20", score)
        return score

    # =========================================================================
    # LAYER 4: EMA STACK  (0–15 pts)
    # =========================================================================

    def _score_ema_stack(
        self,
        direction: str,
        price:    float,
        ema9:     float,
        ema20:    float,
        ema50:    float,
    ) -> int:
        """
        15 pts for EMA 9/20/50 stack alignment.

        Bullish stack: EMA9 > EMA20 > EMA50  → 15 pts
        Partial:       EMA9 > EMA20           →  8 pts
        Just EMA9:     price > EMA9            →  5 pts
        Contradict:    price < EMA9            →  0 pts

        Bear stack: EMA9 < EMA20 < EMA50  → 15 pts (inverted)
        """
        if direction == DIR_BULLISH:
            full_stack = ema9 > ema20 and ema20 > ema50
            mid_stack  = ema9 > ema20
            ema9_ok    = price > ema9

            if full_stack:
                score = 15
            elif mid_stack:
                score = 8
            elif ema9_ok:
                score = 5
            else:
                score = 0

        elif direction == DIR_BEARISH:
            full_stack = ema9 < ema20 and ema20 < ema50
            mid_stack  = ema9 < ema20
            ema9_ok    = price < ema9

            if full_stack:
                score = 15
            elif mid_stack:
                score = 8
            elif ema9_ok:
                score = 5
            else:
                score = 0
        else:
            score = 0

        logger.debug("[SignalEngine] L4 EMA stack: %d/15 (9=%.0f 20=%.0f 50=%.0f)",
                     score, ema9, ema20, ema50)
        return score

    # =========================================================================
    # LAYER 5: MARKET STRUCTURE  (0–10 pts)
    # =========================================================================

    def _score_market_structure(
        self,
        direction:        str,
        market_structure: str,
    ) -> int:
        """
        10 pts: Market structure (HH/HL or LH/LL) confirms direction.

        Confirms   : structure == direction → 10 pts
        Neutral    : structure == SIDEWAYS  →  5 pts (give benefit of doubt)
        Contradicts: structure == opposite  →  0 pts
        """
        if market_structure == direction:
            score = 10
        elif market_structure == DIR_SIDEWAYS:
            score = 5
        else:
            score = 0

        logger.debug("[SignalEngine] L5 Market structure: %d/10 (%s vs %s)",
                     score, market_structure, direction)
        return score

    # =========================================================================
    # LAYER 6: OPENING RANGE  (0–10 pts)
    # =========================================================================

    def _score_orb(
        self,
        direction: str,
        price:     float,
        orb:       Dict,
    ) -> int:
        """
        10 pts: Price has broken out of Opening Range in the signal direction.

        Above ORB high (bull) or below ORB low (bear)  → 10 pts
        Within ORB (consolidation)                      →  5 pts
        Wrong side of ORB (contradicts signal)          →  0 pts
        ORB not yet valid (too early / no data)         →  5 pts (neutral)
        """
        if not orb or not orb.get("valid"):
            logger.debug("[SignalEngine] L6 ORB: not valid → 5/10 (neutral)")
            return 5

        orb_high = orb["orb_high"]
        orb_low  = orb["orb_low"]

        if direction == DIR_BULLISH:
            if price > orb_high:
                score = 10   # Confirmed breakout above ORB
            elif price < orb_low:
                score = 0    # Below ORB = contradicts bull signal
            else:
                score = 5    # Inside ORB
        elif direction == DIR_BEARISH:
            if price < orb_low:
                score = 10   # Confirmed breakdown below ORB
            elif price > orb_high:
                score = 0    # Above ORB = contradicts bear signal
            else:
                score = 5    # Inside ORB
        else:
            score = 0

        logger.debug("[SignalEngine] L6 ORB: %d/10 (price=%.2f H=%.2f L=%.2f)",
                     score, price, orb_high, orb_low)
        return score

    # =========================================================================
    # INTERNAL: EMPTY / FILTERED RESULT
    # =========================================================================

    def _empty_result(
        self,
        mtf:          Dict,
        reason:       str  = "",
        hard_filtered: bool = False,
    ) -> Dict:
        """
        Build a no-trade result maintaining full schema compatibility.
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
            "trade_signal": "NO TRADE",
            "trend":        "SIDEWAYS",
            "stop_loss":    "N/A",
            "target1":      "N/A",
            "target2":      "N/A",
            "target3":      "N/A",

            # Zeroed scores
            "base_score":       0,
            "adjusted_score":   0,
            "confidence_level": CONFIDENCE_LOW,

            # Empty analysis
            "oi_result":   {},
            "sent_result": {},
            "scale_up":       False,
            "scale_up_lots":  0,

            # Primary TF shortcuts
            "price": primary.get("price"),
            "vwap":  primary.get("vwap"),
            "ema9":  primary.get("ema9"),

            # New fields
            "dynamic_sl_points":  float(STOP_LOSS_POINTS),
            "score_breakdown":    {},
            "indicators": {
                "rsi":       primary.get("rsi",       50.0),
                "adx":       primary.get("adx",        0.0),
                "atr":       primary.get("atr",        0.0),
                "macd_hist": primary.get("macd_hist",  0.0),
                "ema20":     primary.get("ema20",       0.0),
                "ema50":     primary.get("ema50",       0.0),
                "st_dir":    primary.get("st_dir",      0),
            },
            "hard_filtered":      hard_filtered,
            "hard_filter_reason": reason,
            "market_structure":   mtf.get("market_structure", DIR_SIDEWAYS),
            "candle_pattern":     mtf.get("candle_pattern", {"pattern": "NONE", "bias": "NEUTRAL", "strength": 0}),
            "orb":                mtf.get("orb", {}),

            "_reason": reason,
        }
