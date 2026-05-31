"""
core/indicators.py
==================
Pure-function technical indicator library for TradingBot.

All functions accept a pandas DataFrame with standard OHLCV columns
(Open, High, Low, Close, Volume) and return pandas Series or tuples of Series.

No external TA libraries — all computed in-process from OHLCV data.
NumPy-vectorised where possible; only uses a loop for Supertrend (stateful).

Functions
---------
  compute_rsi              → Series (0-100)
  compute_macd             → (macd, signal, histogram) tuple
  compute_adx              → Series (0-100)
  compute_adx_full         → (adx, +DI, -DI) tuple
  compute_atr              → Series (price units)
  compute_bollinger_bands  → (upper, mid, lower) tuple
  compute_supertrend       → (value, direction) tuple; direction 1=bull -1=bear
  compute_ema              → Series
  compute_opening_range    → dict {orb_high, orb_low, orb_range, valid}
  detect_market_structure  → "BULLISH" | "BEARISH" | "SIDEWAYS"
  detect_candlestick_pattern → dict {pattern, bias, strength}

Usage
-----
    from core.indicators import compute_rsi, compute_adx, compute_atr

    df = data_engine.get_ohlcv("5m")          # raw OHLCV
    df["RSI"] = compute_rsi(df)
    df["ADX"] = compute_adx(df)
    df["ATR"] = compute_atr(df)

PAPER TRADING ONLY — this module computes indicators, it does not execute trades.
"""

import logging
from datetime import time as dt_time
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# =============================================================================
# RSI — Relative Strength Index (Wilder's smoothed method)
# =============================================================================

def compute_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Compute RSI using Wilder's exponential smoothing.

    Range : 0–100
    Overbought threshold : > 70 (default)
    Oversold  threshold  : < 30 (default)

    Parameters
    ----------
    df     : DataFrame with 'Close' column
    period : RSI period (default 14)

    Returns
    -------
    pd.Series of RSI values (NaN filled with neutral 50.0)
    """
    close = df["Close"]
    delta = close.diff()

    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)

    # Wilder's smoothing = EMA with alpha = 1/period
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()

    rs  = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))

    return rsi.fillna(50.0)


# =============================================================================
# MACD — Moving Average Convergence / Divergence
# =============================================================================

def compute_macd(
    df: pd.DataFrame,
    fast: int   = 12,
    slow: int   = 26,
    signal: int = 9,
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """
    Compute MACD line, signal line, and histogram.

    Interpretation
    --------------
    Histogram > 0 : Bullish momentum
    Histogram < 0 : Bearish momentum
    Histogram rising : Accelerating in current direction
    MACD > Signal : Bullish crossover region

    Returns
    -------
    (macd_line, signal_line, histogram)
    """
    close       = df["Close"]
    ema_fast    = close.ewm(span=fast,   adjust=False).mean()
    ema_slow    = close.ewm(span=slow,   adjust=False).mean()
    macd_line   = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram   = macd_line - signal_line

    return macd_line, signal_line, histogram


# =============================================================================
# ADX — Average Directional Index  (+DI, −DI)
# =============================================================================

def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Compute ADX (trend strength only — no direction).

    Range : 0–100
    <  18  : No trend / sideways / choppy  → avoid trading
    18–30  : Emerging trend
    30–50  : Strong trend
    >  50  : Very strong trend (rare)

    Parameters
    ----------
    df     : DataFrame with High, Low, Close
    period : Smoothing period (default 14)

    Returns
    -------
    pd.Series of ADX values (0-100), NaN filled with 0.0
    """
    adx, _, _ = compute_adx_full(df, period)
    return adx


def compute_adx_full(
    df: pd.DataFrame,
    period: int = 14,
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """
    Compute ADX with directional indicators (pure numpy, no pandas fillna/ewm).

    Returns
    -------
    (ADX, +DI, -DI)

    Directional bias:
        +DI > -DI : Bullish directional pressure
        -DI > +DI : Bearish directional pressure
    """
    h = np.asarray(df["High"].values,  dtype=float)
    l = np.asarray(df["Low"].values,   dtype=float)
    c = np.asarray(df["Close"].values, dtype=float)
    n = len(h)
    alpha = 1.0 / period

    # ── True Range ─────────────────────────────────────────────────────────
    tr       = np.zeros(n, dtype=float)
    plus_dm  = np.zeros(n, dtype=float)
    minus_dm = np.zeros(n, dtype=float)

    tr[0] = h[0] - l[0]
    for i in range(1, n):
        tr[i]  = max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1]))
        up   =  h[i] - h[i-1]
        down = -(l[i] - l[i-1])
        if up > down and up > 0:
            plus_dm[i]  = up
        if down > up and down > 0:
            minus_dm[i] = down

    # ── Wilder's smoothing ─────────────────────────────────────────────────
    atr14_arr  = np.zeros(n, dtype=float)
    plus_sm    = np.zeros(n, dtype=float)
    minus_sm   = np.zeros(n, dtype=float)

    atr14_arr[0] = tr[0]
    plus_sm[0]   = plus_dm[0]
    minus_sm[0]  = minus_dm[0]

    for i in range(1, n):
        atr14_arr[i] = alpha * tr[i]       + (1 - alpha) * atr14_arr[i-1]
        plus_sm[i]   = alpha * plus_dm[i]  + (1 - alpha) * plus_sm[i-1]
        minus_sm[i]  = alpha * minus_dm[i] + (1 - alpha) * minus_sm[i-1]

    # ── +DI and -DI ────────────────────────────────────────────────────────
    safe_atr = np.where(atr14_arr == 0, np.nan, atr14_arr)
    plus_di  = 100.0 * plus_sm  / safe_atr
    minus_di = 100.0 * minus_sm / safe_atr

    # ── ADX ────────────────────────────────────────────────────────────────
    di_sum = plus_di + minus_di
    dx = np.where(di_sum == 0, 0.0,
                  100.0 * np.abs(plus_di - minus_di) / np.where(di_sum == 0, np.nan, di_sum))
    dx = np.nan_to_num(dx, nan=0.0)

    adx_arr = np.zeros(n, dtype=float)
    adx_arr[0] = dx[0]
    for i in range(1, n):
        adx_arr[i] = alpha * dx[i] + (1 - alpha) * adx_arr[i-1]

    return (
        pd.Series(np.nan_to_num(adx_arr,   nan=0.0), index=df.index),
        pd.Series(np.nan_to_num(plus_di,   nan=0.0), index=df.index),
        pd.Series(np.nan_to_num(minus_di,  nan=0.0), index=df.index),
    )


# =============================================================================
# ATR — Average True Range  (Wilder's smoothing)
# =============================================================================

def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Compute ATR — measures volatility in price-unit terms.

    Use for:
      - Dynamic stop-loss: SL = ATR * multiplier
      - Position sizing: larger ATR → smaller position
      - Breakout confirmation: price move > 1 ATR is significant

    Parameters
    ----------
    df     : DataFrame with High, Low, Close
    period : ATR period (default 14)

    Returns
    -------
    pd.Series of ATR values (same units as price, e.g. NIFTY points)
    """
    # Pure numpy — avoids all pandas fillna/ewm deprecation warnings
    h = np.asarray(df["High"].values,  dtype=float)
    l = np.asarray(df["Low"].values,   dtype=float)
    c = np.asarray(df["Close"].values, dtype=float)
    n = len(h)

    # True Range
    tr = np.zeros(n, dtype=float)
    tr[0] = h[0] - l[0]
    for i in range(1, n):
        tr[i] = max(h[i] - l[i],
                    abs(h[i] - c[i - 1]),
                    abs(l[i] - c[i - 1]))

    # Wilder's smoothing
    alpha   = 1.0 / period
    atr_arr = np.zeros(n, dtype=float)
    atr_arr[0] = tr[0]
    for i in range(1, n):
        atr_arr[i] = alpha * tr[i] + (1.0 - alpha) * atr_arr[i - 1]

    return pd.Series(atr_arr, index=df.index)


# =============================================================================
# BOLLINGER BANDS
# =============================================================================

def compute_bollinger_bands(
    df: pd.DataFrame,
    period: int  = 20,
    std:    float = 2.0,
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """
    Compute Bollinger Bands.

    Interpretation
    --------------
    BB Width = (upper - lower) / middle × 100
    Narrow (squeeze)    : Expect volatility expansion — potential breakout
    Wide (expansion)    : Trend in progress — ride it
    Price near upper    : Overbought / resistance in range markets
    Price near lower    : Oversold / support in range markets
    Price outside bands : Strong trend / breakout

    Returns
    -------
    (upper_band, middle_band, lower_band)
    """
    close  = df["Close"]
    middle = close.rolling(period).mean()
    std_s  = close.rolling(period).std(ddof=0)

    upper = middle + std * std_s
    lower = middle - std * std_s

    return upper, middle, lower


# =============================================================================
# SUPERTREND  (vectorised with NumPy loop for the stateful part)
# =============================================================================

def compute_supertrend(
    df:         pd.DataFrame,
    period:     int   = 10,
    multiplier: float = 3.0,
) -> Tuple[pd.Series, pd.Series]:
    """
    Compute Supertrend indicator.

    Supertrend is an excellent trend-following filter with less whipsaw
    than a simple moving-average cross. It flips when price crosses the band.

    Parameters
    ----------
    df         : DataFrame with High, Low, Close
    period     : ATR period (default 10)
    multiplier : ATR multiplier for band width (default 3.0)

    Returns
    -------
    (supertrend_values, direction)
    direction: pd.Series where  1 = BULLISH (price above supertrend)
                                -1 = BEARISH (price below supertrend)
    """
    atr  = compute_atr(df, period).values
    high = df["High"].values
    low  = df["Low"].values
    close = df["Close"].values
    n     = len(df)

    hl2         = (high + low) / 2.0
    upper_basic = hl2 + multiplier * atr
    lower_basic = hl2 - multiplier * atr

    upper      = np.zeros(n, dtype=float)
    lower      = np.zeros(n, dtype=float)
    direction  = np.ones(n,  dtype=int)
    supertrend = np.zeros(n, dtype=float)

    # ── Initialise first bar ────────────────────────────────────────────────
    upper[0] = upper_basic[0]
    lower[0] = lower_basic[0]
    if close[0] >= lower_basic[0]:
        direction[0]  = 1
        supertrend[0] = lower_basic[0]
    else:
        direction[0]  = -1
        supertrend[0] = upper_basic[0]

    # ── Stateful loop ───────────────────────────────────────────────────────
    for i in range(1, n):
        # Upper band: tighten only if new basic < prev; widen if price crosses up
        if upper_basic[i] < upper[i - 1] or close[i - 1] > upper[i - 1]:
            upper[i] = upper_basic[i]
        else:
            upper[i] = upper[i - 1]

        # Lower band: tighten only if new basic > prev; widen if price crosses down
        if lower_basic[i] > lower[i - 1] or close[i - 1] < lower[i - 1]:
            lower[i] = lower_basic[i]
        else:
            lower[i] = lower[i - 1]

        # Flip direction
        prev_dir = direction[i - 1]
        if prev_dir == 1:
            direction[i] = -1 if close[i] < lower[i] else 1
        else:
            direction[i] =  1 if close[i] > upper[i] else -1

        # Supertrend level
        supertrend[i] = lower[i] if direction[i] == 1 else upper[i]

    return (
        pd.Series(supertrend, index=df.index),
        pd.Series(direction,  index=df.index),
    )


# =============================================================================
# EMA — generic
# =============================================================================

def compute_ema(df: pd.DataFrame, span: int) -> pd.Series:
    """Compute EMA of given span on Close prices."""
    return df["Close"].ewm(span=span, adjust=False).mean()


# =============================================================================
# OPENING RANGE BREAKOUT (ORB)
# =============================================================================

def compute_opening_range(
    df: pd.DataFrame,
    orb_minutes: int = 15,
) -> Dict:
    """
    Compute the Opening Range for today's NSE session.

    The Opening Range is the high and low of the first `orb_minutes`
    of trading after 09:15 IST.  On 5-minute data, a 15-minute ORB
    spans the first 3 candles (09:15, 09:20, 09:25).

    Interpretation
    --------------
    Price > ORB High  : Bullish breakout  → favour CE trades
    Price < ORB Low   : Bearish breakout  → favour PE trades
    Price inside ORB  : Consolidation — wait for breakout confirmation

    Parameters
    ----------
    df          : 5m OHLCV DataFrame with DatetimeIndex in IST (tz-naive)
    orb_minutes : Minutes after 09:15 that define the ORB (default 15)

    Returns
    -------
    {
        "orb_high":  float | None,
        "orb_low":   float | None,
        "orb_range": float,
        "valid":     bool,
    }
    """
    try:
        if df is None or df.empty:
            return {"orb_high": None, "orb_low": None, "orb_range": 0.0, "valid": False}

        today         = df.index[-1].date()
        today_candles = df[df.index.date == today]

        if today_candles.empty:
            return {"orb_high": None, "orb_low": None, "orb_range": 0.0, "valid": False}

        # ORB window: 09:15:00 → 09:15:00 + orb_minutes
        orb_end_total_min = 9 * 60 + 15 + orb_minutes
        orb_end_hour      = orb_end_total_min // 60
        orb_end_min       = orb_end_total_min % 60

        window = today_candles[
            (today_candles.index.time >= dt_time(9, 15)) &
            (today_candles.index.time <  dt_time(orb_end_hour, orb_end_min))
        ]

        if window.empty:
            return {"orb_high": None, "orb_low": None, "orb_range": 0.0, "valid": False}

        orb_high  = float(window["High"].max())
        orb_low   = float(window["Low"].min())
        orb_range = round(orb_high - orb_low, 2)

        return {
            "orb_high":  round(orb_high,  2),
            "orb_low":   round(orb_low,   2),
            "orb_range": orb_range,
            "valid":     True,
        }

    except Exception as exc:
        logger.warning("[Indicators] ORB computation failed: %s", exc)
        return {"orb_high": None, "orb_low": None, "orb_range": 0.0, "valid": False}


# =============================================================================
# MARKET STRUCTURE — Higher Highs / Higher Lows detection
# =============================================================================

def detect_market_structure(df: pd.DataFrame, lookback: int = 12) -> str:
    """
    Detect recent market structure from the last `lookback` candles.

    Rules
    -----
    BULLISH : Recent swing highs are rising  AND recent swing lows are rising
              (Higher Highs + Higher Lows)
    BEARISH : Recent swing highs are falling AND recent swing lows are falling
              (Lower Highs + Lower Lows)
    SIDEWAYS: Mixed or no clear structure

    Swing detection uses a 2-candle pivot rule:
        Swing High: candle[i] > candle[i±1] and candle[i±2]
        Swing Low:  candle[i] < candle[i±1] and candle[i±2]

    Falls back to a simple first-half / second-half comparison if fewer
    than 2 swing pivots are found.

    Parameters
    ----------
    df       : OHLCV DataFrame
    lookback : Recent candles to scan (default 12)

    Returns
    -------
    "BULLISH" | "BEARISH" | "SIDEWAYS"
    """
    if len(df) < lookback + 4:
        return "SIDEWAYS"

    recent = df.tail(lookback)
    highs  = recent["High"].values
    lows   = recent["Low"].values
    n      = len(highs)

    swing_highs = []
    swing_lows  = []

    for i in range(2, n - 2):
        if (highs[i] > highs[i-1] and highs[i] > highs[i-2] and
                highs[i] > highs[i+1] and highs[i] > highs[i+2]):
            swing_highs.append(highs[i])

        if (lows[i] < lows[i-1] and lows[i] < lows[i-2] and
                lows[i] < lows[i+1] and lows[i] < lows[i+2]):
            swing_lows.append(lows[i])

    # ── Enough swings: compare last two pairs ────────────────────────────────
    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        hh = swing_highs[-1] > swing_highs[-2]
        hl = swing_lows[-1]  > swing_lows[-2]
        lh = swing_highs[-1] < swing_highs[-2]
        ll = swing_lows[-1]  < swing_lows[-2]

        if hh and hl:
            return "BULLISH"
        if lh and ll:
            return "BEARISH"
        return "SIDEWAYS"

    # ── Fallback: first-half vs second-half comparison ────────────────────────
    mid  = n // 2
    fh   = recent.iloc[:mid]
    sh   = recent.iloc[mid:]

    fh_high  = float(fh["High"].max())
    sh_high  = float(sh["High"].max())
    fh_low   = float(fh["Low"].min())
    sh_low   = float(sh["Low"].min())

    hh = sh_high > fh_high
    hl = sh_low  > fh_low
    lh = sh_high < fh_high
    ll = sh_low  < fh_low

    if hh and hl:
        return "BULLISH"
    if lh and ll:
        return "BEARISH"
    return "SIDEWAYS"


# =============================================================================
# CANDLESTICK PATTERNS — last two candles
# =============================================================================

def detect_candlestick_pattern(df: pd.DataFrame) -> Dict:
    """
    Detect the most recent actionable candlestick pattern.

    Patterns detected (in priority order)
    --------------------------------------
    Bullish Engulfing  — strong bullish reversal / continuation
    Bearish Engulfing  — strong bearish reversal / continuation
    Hammer             — bullish reversal, long lower wick
    Shooting Star      — bearish reversal, long upper wick
    Bull Marubozu      — strong bullish candle, no wicks
    Bear Marubozu      — strong bearish candle, no wicks
    Doji               — indecision / reversal warning
    NONE               — no significant pattern

    Parameters
    ----------
    df : OHLCV DataFrame (at least 2 rows)

    Returns
    -------
    {
        "pattern":  str,  # Pattern name
        "bias":     str,  # "BULLISH" | "BEARISH" | "NEUTRAL"
        "strength": int,  # 1=weak  2=moderate  3=strong
    }
    """
    if len(df) < 2:
        return {"pattern": "NONE", "bias": "NEUTRAL", "strength": 0}

    curr = df.iloc[-1]
    prev = df.iloc[-2]

    # ── Current candle geometry ───────────────────────────────────────────────
    c_o = float(curr["Open"])
    c_h = float(curr["High"])
    c_l = float(curr["Low"])
    c_c = float(curr["Close"])

    c_range = c_h - c_l
    if c_range < 0.0001:
        return {"pattern": "NONE", "bias": "NEUTRAL", "strength": 0}

    c_body      = abs(c_c - c_o)
    body_pct    = c_body / c_range
    upper_wick  = c_h - max(c_o, c_c)
    lower_wick  = min(c_o, c_c) - c_l
    is_bull     = c_c > c_o
    is_bear     = c_c < c_o

    # ── Previous candle geometry ─────────────────────────────────────────────
    p_o = float(prev["Open"])
    p_c = float(prev["Close"])
    p_h = float(prev["High"])
    p_l = float(prev["Low"])
    p_body = abs(p_c - p_o)

    # ── Doji — very small body ────────────────────────────────────────────────
    if body_pct < 0.06:
        return {"pattern": "DOJI", "bias": "NEUTRAL", "strength": 1}

    # ── Bullish Engulfing ─────────────────────────────────────────────────────
    if (is_bull and p_c < p_o                         # Previous was bearish
            and c_o  <= p_c                           # Opens at or below prev close
            and c_c  >= p_o                           # Closes at or above prev open
            and c_body >= p_body * 0.75):             # Body engulfs at least 75%
        return {"pattern": "BULLISH_ENGULFING", "bias": "BULLISH", "strength": 3}


    # -- Bearish Engulfing -------------------------------------------------
    if (is_bear and p_c > p_o                          # Previous was bullish
            and c_o  >= p_c                            # Opens at or above prev close
            and c_c  <= p_o                            # Closes at or below prev open
            and c_body >= p_body * 0.75):              # Body engulfs at least 75%
        return {"pattern": "BEARISH_ENGULFING", "bias": "BEARISH", "strength": 3}

    # -- Hammer -- bullish reversal (long lower wick, small body near top) -
    if (lower_wick >= c_body * 2.0
            and upper_wick <= c_body * 0.4
            and body_pct >= 0.10):
        return {"pattern": "HAMMER", "bias": "BULLISH", "strength": 2}

    # -- Shooting Star -- bearish reversal (long upper wick, small body) ---
    if (upper_wick >= c_body * 2.0
            and lower_wick <= c_body * 0.4
            and body_pct >= 0.10):
        return {"pattern": "SHOOTING_STAR", "bias": "BEARISH", "strength": 2}

    # -- Bull Marubozu -- strong momentum candle, tiny/no wicks -----------
    if (is_bull
            and body_pct >= 0.80
            and upper_wick <= c_range * 0.05
            and lower_wick <= c_range * 0.05):
        return {"pattern": "BULL_MARUBOZU", "bias": "BULLISH", "strength": 3}

    # -- Bear Marubozu -- strong bearish momentum candle ------------------
    if (is_bear
            and body_pct >= 0.80
            and upper_wick <= c_range * 0.05
            and lower_wick <= c_range * 0.05):
        return {"pattern": "BEAR_MARUBOZU", "bias": "BEARISH", "strength": 3}

    # -- Pin Bar Bullish -- tail >= 2.5x body, points down ----------------
    if (lower_wick >= c_body * 2.5
            and upper_wick <= c_range * 0.15):
        return {"pattern": "PIN_BAR_BULL", "bias": "BULLISH", "strength": 2}

    # -- Pin Bar Bearish -- tail >= 2.5x body, points up ------------------
    if (upper_wick >= c_body * 2.5
            and lower_wick <= c_range * 0.15):
        return {"pattern": "PIN_BAR_BEAR", "bias": "BEARISH", "strength": 2}

    # -- Inside Bar -- current candle fully inside previous ---------------
    if (c_h <= p_h and c_l >= p_l):
        return {"pattern": "INSIDE_BAR", "bias": "NEUTRAL", "strength": 1}

    # -- No significant pattern -------------------------------------------
    return {"pattern": "NONE", "bias": "NEUTRAL", "strength": 0}
