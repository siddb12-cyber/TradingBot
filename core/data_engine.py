"""
core/data_engine.py
===================
API-based market data layer for TradingBot.

Replaces the TradingView + Playwright + OCR pipeline with direct API calls:
  - Live NIFTY price    : NSE Index API (fast, real-time, no auth)
  - OHLCV candle data   : yfinance (reliable, free, no API key required)
  - Technical indicators: EMA9 and VWAP derived in-process from candle data

Why this replaces OCR
---------------------
OCR on TradingView screenshots is fragile:
  - Requires Chrome + TradingView running 24/7
  - Crashes when browser memory grows
  - Screenshot timing errors cause misreads
  - Values are stale by the time OCR finishes

yfinance + NSE API is:
  - Zero-dependency on browser processes
  - Self-contained in a single Python call
  - Reliable fallback chain (NSE → yfinance)
  - Identical output schema to the old MultiTimeframeAnalyzer

Architecture
------------
  DataEngine
  ├── get_live_price()   → float               (NSE API → yfinance fallback)
  ├── get_ohlcv(tf)      → pd.DataFrame         (cached 60s per TF)
  └── get_analysis()     → dict                 (MTF signal, cached 60s per TF)

get_analysis() output is a **drop-in replacement** for MultiTimeframeAnalyzer.analyze().
The output schema is identical — all downstream modules (DecisionLogger, RiskEngine,
SignalEngine, ai_trading_assistant) work without modification.

Cache Design
------------
  - OHLCV: one CacheSlot per timeframe, TTL = DATA_CACHE_SECONDS (60s)
  - Live price: one CacheSlot, TTL = LIVE_PRICE_CACHE_SECONDS (10s)
  - Thread-safe (threading.Lock per slot)
  - clear_cache() forces refresh on demand

Thread Safety
-------------
All public methods acquire per-slot locks. Designed for use in
trading_engine.py's multi-threaded architecture (signal_loop, tracker_loop
and telegram_poller can all call DataEngine concurrently).
"""

import logging
import threading
import time
from typing import Dict, Optional

import pandas as pd
import requests
import yfinance as yf

from config.settings import (
    # NSE API
    NSE_INDEX_URL,
    NSE_API_HEADERS,
    # Ticker and cache config
    NIFTY_TICKER,
    DATA_CACHE_SECONDS,
    LIVE_PRICE_CACHE_SECONDS,
    YFINANCE_TF_PARAMS,
    DATA_MIN_CANDLES,
    # Strategy config
    TIMEFRAMES,
    PRIMARY_TIMEFRAME,
    NIFTY_PRICE_MIN,
    NIFTY_PRICE_MAX,
    # Indicator periods
    RSI_PERIOD,
    MACD_FAST, MACD_SLOW, MACD_SIGNAL,
    ADX_PERIOD,
    ATR_PERIOD,
    EMA_SHORT, EMA_MID, EMA_LONG,
    BB_PERIOD, BB_STD,
    SUPERTREND_PERIOD, SUPERTREND_MULTIPLIER,
    ORB_MINUTES,
    MARKET_STRUCTURE_LOOKBACK,
)
from core.indicators import (
    compute_rsi,
    compute_macd,
    compute_adx,
    compute_atr,
    compute_bollinger_bands,
    compute_supertrend,
    compute_ema,
    compute_opening_range,
    detect_market_structure,
    detect_candlestick_pattern,
)

# =========================
# MODULE LOGGER
# =========================

logger = logging.getLogger(__name__)


# =========================
# INDICATOR COMPUTATIONS
# =========================

# =========================
# LEGACY WRAPPERS (kept for backward compatibility)
# =========================

def compute_ema9(df: pd.DataFrame) -> pd.Series:
    """EMA9 — thin wrapper around compute_ema for compatibility."""
    return compute_ema(df, EMA_SHORT)


def compute_vwap(df: pd.DataFrame) -> pd.Series:
    """
    Compute VWAP (Volume Weighted Average Price), reset at each calendar day.

    Formula per candle (when volume is available):
        cumsum((H+L+C)/3 * Volume) / cumsum(Volume)

    Fallback for index instruments (e.g. ^NSEI) with zero volume:
        cumulative mean of (H+L+C)/3 — TWAP (Time-Weighted Average Price).
        This is standard practice for instruments that carry no native volume
        and produces meaningful intraday deviation from price, unlike the
        NaN-then-fallback-to-close behaviour of the pure volume formula.

    The VWAP is reset at the start of each new trading day using
    df.index.normalize() (strips the time component) to group candles by date.

    Parameters
    ----------
    df : pd.DataFrame
        Must contain columns: High, Low, Close, Volume.
        Index must be a DatetimeIndex (tz-naive or tz-aware).

    Returns
    -------
    pd.Series of VWAP values aligned to df.index.
    """
    typical_price = (df["High"] + df["Low"] + df["Close"]) / 3.0

    # Group by calendar date (handles multi-day DataFrames)
    day_key     = df.index.normalize()
    vwap_series = pd.Series(index=df.index, dtype=float)

    for day in day_key.unique():
        mask    = day_key == day
        tp_day  = typical_price[mask]
        vol_day = df["Volume"][mask]
        cum_vol = vol_day.cumsum()

        if cum_vol.iloc[-1] > 0:
            # ---- Standard VWAP: volume-weighted typical price ----
            cum_tp_vol        = (tp_day * vol_day).cumsum()
            vwap_series[mask] = cum_tp_vol / cum_vol.replace(0, float("nan"))
        else:
            # ---- TWAP fallback: used for indices (e.g. ^NSEI) with no volume ----
            # Cumulative mean of HLC3 — resets each day, diverges from price
            # as the session progresses, giving a meaningful vs-VWAP reading.
            vwap_series[mask] = tp_day.expanding().mean()

    return vwap_series




# =========================
# DIRECTION CLASSIFIER
# =========================

def _determine_direction(price: float, vwap: float, ema9: float) -> str:
    """
    Classify market direction from price vs VWAP and EMA9.

    Rules (identical to original OCR-based trend_decision_engine.py):
      BULLISH  : price > vwap  AND  price > ema9
      BEARISH  : price < vwap  AND  price < ema9
      SIDEWAYS : any mixed condition (one above, one below)

    Parameters
    ----------
    price, vwap, ema9 : float — latest candle values

    Returns
    -------
    "BULLISH" | "BEARISH" | "SIDEWAYS"
    """
    if price > vwap and price > ema9:
        return "BULLISH"
    elif price < vwap and price < ema9:
        return "BEARISH"
    else:
        return "SIDEWAYS"


# =========================
# TTL CACHE SLOT
# =========================

class _CacheSlot:
    """
    Thread-safe TTL cache for a single value.

    get()  → returns cached value if still within TTL, else None.
    set()  → stores value and records timestamp.
    clear() → invalidates the cache immediately.
    """

    def __init__(self, ttl_seconds: int) -> None:
        self._ttl   = ttl_seconds
        self._value = None
        self._ts    = 0.0
        self._lock  = threading.Lock()

    def get(self):
        with self._lock:
            if self._value is not None and (time.time() - self._ts) < self._ttl:
                return self._value
            return None

    def set(self, value) -> None:
        with self._lock:
            self._value = value
            self._ts    = time.time()

    def clear(self) -> None:
        with self._lock:
            self._value = None
            self._ts    = 0.0


# =========================
# DATA ENGINE CLASS
# =========================

class DataEngine:
    """
    Central market data provider — API-based, no browser dependency.

    Instantiate once and share across all threads in trading_engine.py.

    Quick reference
    ---------------
    engine = DataEngine()

    price  = engine.get_live_price()     # Latest NIFTY spot (float | None)
    df_5m  = engine.get_ohlcv("5m")      # 5m OHLCV + EMA9 + VWAP (DataFrame | None)
    signal = engine.get_analysis()       # Multi-TF analysis dict
    health = engine.health_check()       # Startup diagnostics
    engine.clear_cache()                 # Force data refresh
    """

    def __init__(self) -> None:
        # ---- Per-timeframe OHLCV caches ----
        self._ohlcv_cache: Dict[str, _CacheSlot] = {
            tf: _CacheSlot(DATA_CACHE_SECONDS) for tf in TIMEFRAMES
        }

        # ---- Live price cache (shorter TTL than OHLCV) ----
        self._price_cache = _CacheSlot(LIVE_PRICE_CACHE_SECONDS)

        # ---- NSE HTTP session (keeps cookies for NSE API) ----
        self._nse_session: Optional[requests.Session] = None
        self._nse_session_lock = threading.Lock()

        logger.info(
            "[DataEngine] Initialised | Ticker=%s | OHLCV cache=%ds | Price cache=%ds | TFs=%s",
            NIFTY_TICKER, DATA_CACHE_SECONDS, LIVE_PRICE_CACHE_SECONDS, TIMEFRAMES,
        )

    # ------------------------------------------------------------------
    # PUBLIC: LIVE PRICE
    # ------------------------------------------------------------------

    def get_live_price(self) -> Optional[float]:
        """
        Return the latest NIFTY 50 spot price.

        Source priority:
          1. NSE Index API  — fastest, updated every few seconds
          2. yfinance       — fallback if NSE API is unreachable

        Caches result for LIVE_PRICE_CACHE_SECONDS (10s) to avoid
        hammering the API when the tracker loop calls this every 60s.

        Returns
        -------
        float  — NIFTY spot price (validated within NIFTY_PRICE_MIN..MAX)
        None   — if both sources fail
        """
        # ---- Return from cache if still fresh ----
        cached = self._price_cache.get()
        if cached is not None:
            return cached

        # ---- Try NSE API first ----
        price = self._price_from_nse()

        # ---- Fall back to yfinance ----
        if price is None:
            logger.warning("[DataEngine] NSE live price unavailable — trying yfinance fallback")
            price = self._price_from_yfinance()

        if price is not None:
            self._price_cache.set(price)
            logger.debug("[DataEngine] Live NIFTY price: %.2f", price)
        else:
            logger.error("[DataEngine] Both NSE and yfinance price sources failed")

        return price

    # ------------------------------------------------------------------
    # PUBLIC: OHLCV DATA
    # ------------------------------------------------------------------

    def get_ohlcv(self, timeframe: str) -> Optional[pd.DataFrame]:
        """
        Return OHLCV DataFrame with EMA9 and VWAP appended.

        Output columns: Open, High, Low, Close, Volume, EMA9, VWAP
        Index: tz-naive DatetimeIndex in IST (Asia/Kolkata)

        Data is fetched from yfinance using the period and interval
        defined in YFINANCE_TF_PARAMS for the given timeframe.

        Parameters
        ----------
        timeframe : "5m", "15m", or "1h" (must be in YFINANCE_TF_PARAMS)

        Returns
        -------
        pd.DataFrame | None (None on fetch failure or insufficient rows)
        """
        if timeframe not in YFINANCE_TF_PARAMS:
            logger.error("[DataEngine] Unknown timeframe '%s' — must be one of %s", timeframe, list(YFINANCE_TF_PARAMS.keys()))
            return None

        # ---- Return cached data if fresh ----
        cached = self._ohlcv_cache[timeframe].get()
        if cached is not None:
            return cached

        # ---- Fetch from yfinance ----
        df = self._fetch_yfinance(timeframe)
        if df is None or df.empty:
            logger.warning("[DataEngine] OHLCV fetch returned no data for %s", timeframe)
            return None

        if len(df) < DATA_MIN_CANDLES:
            logger.warning(
                "[DataEngine] Only %d candles for %s (min=%d) — insufficient for indicators",
                len(df), timeframe, DATA_MIN_CANDLES,
            )
            return None

        # ---- Compute indicators in-place ----
        # Core (existing)
        df["EMA9"] = compute_ema(df, EMA_SHORT)
        df["VWAP"] = compute_vwap(df)

        # EMA stack
        df["EMA20"] = compute_ema(df, EMA_MID)
        df["EMA50"] = compute_ema(df, EMA_LONG)

        # Momentum
        try:
            df["RSI"] = compute_rsi(df, RSI_PERIOD)
        except Exception as _e:
            logger.warning("[DataEngine] RSI failed (%s): %s", timeframe, _e)
            df["RSI"] = 50.0

        try:
            _macd, _sig, _hist = compute_macd(df, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
            df["MACD"]      = _macd
            df["MACD_SIG"]  = _sig
            df["MACD_HIST"] = _hist
        except Exception as _e:
            logger.warning("[DataEngine] MACD failed (%s): %s", timeframe, _e)
            df["MACD"] = df["MACD_SIG"] = df["MACD_HIST"] = 0.0

        # Trend strength
        try:
            df["ADX"] = compute_adx(df, ADX_PERIOD)
        except Exception as _e:
            logger.warning("[DataEngine] ADX failed (%s): %s", timeframe, _e)
            df["ADX"] = 0.0

        # Volatility
        try:
            df["ATR"] = compute_atr(df, ATR_PERIOD)
        except Exception as _e:
            logger.warning("[DataEngine] ATR failed (%s): %s", timeframe, _e)
            df["ATR"] = 0.0

        try:
            _bb_u, _bb_m, _bb_l = compute_bollinger_bands(df, BB_PERIOD, BB_STD)
            df["BB_UPPER"] = _bb_u
            df["BB_MID"]   = _bb_m
            df["BB_LOWER"] = _bb_l
        except Exception as _e:
            logger.warning("[DataEngine] BB failed (%s): %s", timeframe, _e)
            df["BB_UPPER"] = df["BB_MID"] = df["BB_LOWER"] = df["Close"]

        # Supertrend
        try:
            _st_val, _st_dir = compute_supertrend(df, SUPERTREND_PERIOD, SUPERTREND_MULTIPLIER)
            df["ST_VAL"] = _st_val
            df["ST_DIR"] = _st_dir   # 1 = bullish, -1 = bearish
        except Exception as _e:
            logger.warning("[DataEngine] Supertrend failed (%s): %s", timeframe, _e)
            df["ST_VAL"] = df["Close"]
            df["ST_DIR"] = 0.0

        # ---- Cache and return ----
        self._ohlcv_cache[timeframe].set(df)
        logger.debug(
            "[DataEngine] OHLCV cached | TF=%s | Rows=%d | Last candle=%s",
            timeframe, len(df),
            df.index[-1].strftime("%Y-%m-%d %H:%M") if len(df) else "N/A",
        )
        return df

    # ------------------------------------------------------------------
    # PUBLIC: MULTI-TIMEFRAME ANALYSIS
    # ------------------------------------------------------------------

    def get_analysis(self) -> Dict:
        """
        Compute multi-timeframe (MTF) analysis across all configured timeframes.

        This is a **drop-in replacement** for MultiTimeframeAnalyzer.analyze().
        The output schema is identical so all downstream code (DecisionLogger,
        SignalEngine, ai_trading_assistant) requires no changes.

        Output schema
        -------------
        {
          "valid"            : bool,       # False if any TF had no data
          "is_trade"         : bool,       # True if primary TF is not SIDEWAYS
          "direction"        : str,        # "BULLISH" | "BEARISH" | "SIDEWAYS"
          "alignment_count"  : int,        # 0–3  (how many TFs agree with primary)
          "alignment_summary": str,        # Human-readable e.g. "3/3 BULLISH aligned"
          "timeframe_data"   : {
              "5m":  {"price": float, "vwap": float, "ema9": float, "direction": str},
              "15m": {...},
              "1h":  {...},
          }
        }

        Returns
        -------
        dict — always returns a dict (never raises, never returns None)
        On failure: valid=False, is_trade=False, direction="SIDEWAYS"
        """
        timeframe_data: Dict[str, Dict] = {}
        directions:     Dict[str, str]  = {}
        any_invalid = False

        # ---- Collect per-timeframe values ----
        for tf in TIMEFRAMES:
            df = self.get_ohlcv(tf)

            if df is None or df.empty or len(df) < 2:
                logger.warning("[DataEngine] No usable data for TF=%s — flagging as INVALID", tf)
                any_invalid = True
                timeframe_data[tf] = {
                    "price":     None,
                    "vwap":      None,
                    "ema9":      None,
                    "direction": "N/A",
                }
                directions[tf] = "N/A"
                continue

            # ---- Use last completed candle ----
            last  = df.iloc[-1]
            price = float(last["Close"])
            vwap  = float(last["VWAP"])  if not pd.isna(last["VWAP"])  else price
            ema9  = float(last["EMA9"])   if not pd.isna(last["EMA9"])   else price
            ema20 = float(last["EMA20"])  if not pd.isna(last["EMA20"])  else price
            ema50 = float(last["EMA50"])  if not pd.isna(last["EMA50"])  else price
            rsi   = float(last["RSI"])    if not pd.isna(last["RSI"])    else 50.0
            adx   = float(last["ADX"])    if not pd.isna(last["ADX"])    else 0.0
            atr   = float(last["ATR"])    if not pd.isna(last["ATR"])    else 0.0
            macd_hist = float(last["MACD_HIST"]) if not pd.isna(last["MACD_HIST"]) else 0.0
            macd_line = float(last["MACD"])      if not pd.isna(last["MACD"])      else 0.0
            bb_upper  = float(last["BB_UPPER"])  if not pd.isna(last["BB_UPPER"])  else price
            bb_lower  = float(last["BB_LOWER"])  if not pd.isna(last["BB_LOWER"])  else price
            bb_mid    = float(last["BB_MID"])    if not pd.isna(last["BB_MID"])    else price
            st_dir    = int(last["ST_DIR"])      if not pd.isna(last["ST_DIR"])    else 0

            # BB percentage (0=at lower, 1=at upper, 0.5=at mid)
            bb_width = bb_upper - bb_lower
            bb_pct   = round((price - bb_lower) / bb_width, 3) if bb_width > 0 else 0.5

            direction = _determine_direction(price, vwap, ema9)

            timeframe_data[tf] = {
                "price":     round(price, 2),
                "vwap":      round(vwap,  2),
                "ema9":      round(ema9,  2),
                "ema20":     round(ema20, 2),
                "ema50":     round(ema50, 2),
                "rsi":       round(rsi,   1),
                "adx":       round(adx,   1),
                "atr":       round(atr,   2),
                "macd_hist": round(macd_hist, 3),
                "macd_line": round(macd_line, 3),
                "bb_upper":  round(bb_upper, 2),
                "bb_lower":  round(bb_lower, 2),
                "bb_pct":    bb_pct,
                "st_dir":    st_dir,
                "direction": direction,
            }
            directions[tf] = direction

        # ---- Return invalid result if any TF failed ----
        if any_invalid:
            return {
                "valid":             False,
                "is_trade":          False,
                "direction":         "SIDEWAYS",
                "alignment_count":   0,
                "alignment_summary": "INVALID — data fetch error on one or more timeframes",
                "timeframe_data":    timeframe_data,
                "orb":               {"valid": False, "orb_high": None, "orb_low": None, "orb_range": 0.0},
                "market_structure":  "SIDEWAYS",
                "candle_pattern":    {"pattern": "NONE", "bias": "NEUTRAL", "strength": 0},
            }

        # ---- Determine primary direction and alignment ----
        primary_dir     = directions.get(PRIMARY_TIMEFRAME, "SIDEWAYS")
        aligned_tfs     = [tf for tf in TIMEFRAMES if directions[tf] == primary_dir and directions[tf] != "SIDEWAYS"]
        alignment_count = len(aligned_tfs)

        # ---- Build human-readable alignment string ----
        tf_breakdown      = " | ".join(f"{tf}:{directions[tf]}" for tf in TIMEFRAMES)
        alignment_summary = (
            f"{alignment_count}/{len(TIMEFRAMES)} {primary_dir} aligned | {tf_breakdown}"
        )

        is_trade = primary_dir != "SIDEWAYS"

        # ---- Fetch 5m DataFrame once for ORB + structure + candle ----
        # (already cached from the loop above — no extra network call)
        df_5m = None
        try:
            df_5m = self.get_ohlcv(PRIMARY_TIMEFRAME)
        except Exception as _exc:
            logger.debug("[DataEngine] 5m fetch for ORB/MS skipped: %s", _exc)

        # ---- Opening Range Breakout ----
        orb: dict = {"valid": False, "orb_high": None, "orb_low": None, "orb_range": 0.0}
        try:
            if df_5m is not None:
                orb = compute_opening_range(df_5m, ORB_MINUTES)
        except Exception as _exc:
            logger.debug("[DataEngine] ORB computation failed: %s", _exc)

        # ---- Market Structure (last MARKET_STRUCTURE_LOOKBACK candles) ----
        market_structure = "SIDEWAYS"
        try:
            if df_5m is not None and len(df_5m) >= MARKET_STRUCTURE_LOOKBACK + 4:
                market_structure = detect_market_structure(df_5m, MARKET_STRUCTURE_LOOKBACK)
        except Exception as _exc:
            logger.debug("[DataEngine] Market structure failed: %s", _exc)

        # ---- Candlestick pattern (last 2 candles) ----
        candle_pattern: dict = {"pattern": "NONE", "bias": "NEUTRAL", "strength": 0}
        try:
            if df_5m is not None and len(df_5m) >= 2:
                candle_pattern = detect_candlestick_pattern(df_5m)
        except Exception as _exc:
            logger.debug("[DataEngine] Candlestick pattern failed: %s", _exc)

        logger.info(
            "[DataEngine] MTF | Dir=%s | Align=%d/%d | ADX=%.1f | RSI=%.1f | ORB=%s | MS=%s | %s",
            primary_dir, alignment_count, len(TIMEFRAMES),
            timeframe_data.get(PRIMARY_TIMEFRAME, {}).get("adx", 0),
            timeframe_data.get(PRIMARY_TIMEFRAME, {}).get("rsi", 50),
            "valid" if orb.get("valid") else "N/A",
            market_structure,
            tf_breakdown,
        )

        return {
            "valid":             True,
            "is_trade":          is_trade,
            "direction":         primary_dir,
            "alignment_count":   alignment_count,
            "alignment_summary": alignment_summary,
            "timeframe_data":    timeframe_data,
            "orb":               orb,
            "market_structure":  market_structure,
            "candle_pattern":    candle_pattern,
        }

    # ------------------------------------------------------------------
    # INTERNAL: NSE LIVE PRICE
    # ------------------------------------------------------------------

    def _get_nse_session(self) -> requests.Session:
        """
        Return a persistent requests.Session with NSE cookies.
        Creates a new session if one doesn't exist yet.
        NSE requires a homepage visit to get valid session cookies.
        """
        with self._nse_session_lock:
            if self._nse_session is None:
                session = requests.Session()
                session.headers.update(NSE_API_HEADERS)
                try:
                    # NSE rejects API calls without a valid session cookie.
                    # Visiting the homepage first establishes the session.
                    session.get("https://www.nseindia.com", timeout=8)
                    logger.debug("[DataEngine] NSE session established")
                except Exception as warmup_err:
                    logger.warning("[DataEngine] NSE session warmup failed: %s", warmup_err)
                self._nse_session = session
            return self._nse_session

    def _price_from_nse(self) -> Optional[float]:
        """
        Fetch live NIFTY 50 price from NSE /api/allIndices.

        The NSE API returns a JSON list of all indices. We look for
        the entry where index == "NIFTY 50" and read the "last" field.

        Returns None on any network, parse, or validation error.
        The session is reset on error so the next call gets a fresh one.
        """
        try:
            session  = self._get_nse_session()
            response = session.get(NSE_INDEX_URL, timeout=8)
            response.raise_for_status()
            data = response.json()

            for item in data.get("data", []):
                if item.get("index", "").upper() == "NIFTY 50":
                    raw_price = item.get("last") or item.get("lastPrice")
                    if raw_price is None:
                        logger.warning("[DataEngine] NSE response has no 'last' field for NIFTY 50")
                        return None

                    last_price = float(str(raw_price).replace(",", ""))

                    # Validate price within acceptable bounds
                    if NIFTY_PRICE_MIN <= last_price <= NIFTY_PRICE_MAX:
                        return last_price
                    else:
                        logger.warning(
                            "[DataEngine] NSE price %.2f out of valid range [%.0f–%.0f]",
                            last_price, NIFTY_PRICE_MIN, NIFTY_PRICE_MAX,
                        )
                        return None

            logger.warning("[DataEngine] 'NIFTY 50' not found in NSE allIndices response")
            return None

        except Exception as exc:
            logger.warning("[DataEngine] NSE API fetch error: %s", exc)
            # Reset the session so next call gets a fresh one with new cookies
            with self._nse_session_lock:
                self._nse_session = None
            return None

    def _price_from_yfinance(self) -> Optional[float]:
        """
        Fetch NIFTY live price from yfinance as a fallback.
        Downloads the last 1-minute candle from today's session.

        Returns None on failure or if price is out of bounds.
        """
        try:
            ticker = yf.Ticker(NIFTY_TICKER)
            df     = ticker.history(period="1d", interval="1m")
            if df is None or df.empty:
                logger.warning("[DataEngine] yfinance price fallback returned no data")
                return None

            price = float(df["Close"].iloc[-1])
            if NIFTY_PRICE_MIN <= price <= NIFTY_PRICE_MAX:
                return price

            logger.warning("[DataEngine] yfinance price %.2f out of valid range", price)
            return None

        except Exception as exc:
            logger.warning("[DataEngine] yfinance price fallback error: %s", exc)
            return None

    # ------------------------------------------------------------------
    # INTERNAL: YFINANCE OHLCV FETCH
    # ------------------------------------------------------------------

    def _fetch_yfinance(self, timeframe: str) -> Optional[pd.DataFrame]:
        """
        Download OHLCV data from yfinance.

        Uses the (period, interval) pair from YFINANCE_TF_PARAMS for
        the requested timeframe. Cleans the result:
          - Drops rows with NaN in OHLC columns
          - Converts tz-aware DatetimeIndex to tz-naive IST (Asia/Kolkata)
          - Keeps only standard columns: Open, High, Low, Close, Volume

        Parameters
        ----------
        timeframe : key in YFINANCE_TF_PARAMS ("5m", "15m", "1h")

        Returns
        -------
        pd.DataFrame | None
        """
        period, interval = YFINANCE_TF_PARAMS[timeframe]

        try:
            ticker = yf.Ticker(NIFTY_TICKER)
            df     = ticker.history(period=period, interval=interval)

            if df is None or df.empty:
                logger.warning(
                    "[DataEngine] yfinance returned empty DataFrame for "
                    "%s (period=%s, interval=%s)",
                    NIFTY_TICKER, period, interval,
                )
                return None

            # ---- Keep only standard OHLCV columns ----
            # yfinance sometimes returns extra columns (Dividends, Stock Splits)
            available = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
            df = df[available].copy()

            # ---- Drop corrupted candles ----
            df.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)

            # ---- Normalise timezone → tz-naive IST ----
            if df.index.tz is not None:
                df.index = df.index.tz_convert("Asia/Kolkata").tz_localize(None)

            # ---- Volume: fill NaN with 0 (^NSEI sometimes has no volume) ----
            if "Volume" in df.columns:
                df["Volume"] = df["Volume"].fillna(0)

            logger.debug(
                "[DataEngine] yfinance OK | TF=%s | Rows=%d | First=%s | Last=%s",
                timeframe, len(df),
                df.index[0].strftime("%Y-%m-%d %H:%M") if len(df) else "N/A",
                df.index[-1].strftime("%Y-%m-%d %H:%M") if len(df) else "N/A",
            )
            return df

        except Exception as exc:
            logger.error("[DataEngine] yfinance OHLCV fetch error (%s): %s", timeframe, exc)
            return None

    # ------------------------------------------------------------------
    # UTILITY
    # ------------------------------------------------------------------

    def clear_cache(self) -> None:
        """
        Force-invalidate all caches.
        Call this after a long pause or market open to get fresh data immediately.
        """
        self._price_cache.clear()
        for tf, slot in self._ohlcv_cache.items():
            slot.clear()
        logger.info("[DataEngine] All caches cleared — next call will fetch fresh data")

    def health_check(self) -> Dict:
        """
        Validate connectivity to both data sources.
        Called by trading_engine.py at startup before the main loops begin.

        Returns
        -------
        dict with keys:
          "nse_live_price"    : float | None
          "yfinance_5m_rows"  : int | None
          "status"            : "HEALTHY" | "DEGRADED" | "UNHEALTHY"
          "nse_error"         : str (only present on NSE failure)
          "yfinance_error"    : str (only present on yfinance failure)
        """
        result: Dict = {}

        # ---- Test NSE API ----
        try:
            price = self._price_from_nse()
            result["nse_live_price"] = price
            if price:
                logger.info("[DataEngine] Health: NSE price OK → %.2f", price)
            else:
                logger.warning("[DataEngine] Health: NSE price returned None")
        except Exception as exc:
            result["nse_live_price"] = None
            result["nse_error"]      = str(exc)
            logger.warning("[DataEngine] Health: NSE error → %s", exc)

        # ---- Test yfinance ----
        try:
            df = self._fetch_yfinance("5m")
            rows = len(df) if df is not None else 0
            result["yfinance_5m_rows"] = rows
            if rows:
                logger.info("[DataEngine] Health: yfinance 5m OK → %d rows", rows)
            else:
                logger.warning("[DataEngine] Health: yfinance 5m returned 0 rows")
        except Exception as exc:
            result["yfinance_5m_rows"] = None
            result["yfinance_error"]   = str(exc)
            logger.warning("[DataEngine] Health: yfinance error → %s", exc)

        # ---- Determine overall status ----
        nse_ok  = bool(result.get("nse_live_price"))
        yfin_ok = bool(result.get("yfinance_5m_rows"))

        if nse_ok and yfin_ok:
            result["status"] = "HEALTHY"
        elif yfin_ok:
            result["status"] = "DEGRADED (NSE down — live price from yfinance fallback)"
        else:
            result["status"] = "UNHEALTHY (both sources failed — no data available)"

        logger.info("[DataEngine] Health status: %s", result["status"])
        return result
