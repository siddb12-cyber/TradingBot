"""
core/news_sentiment.py
======================
News Sentiment Engine — four data sources, one unified confidence adjustment.

Sources (all free, no API keys):
  1. India VIX      — from NSE allIndices JSON API
  2. US Futures     — S&P 500 E-mini via yfinance (ES=F)
  3. RSS Feeds      — ET Markets, Moneycontrol, CNBCTV18
  4. Google News    — NIFTY-specific RSS

Score adjustments applied to base MTF confidence score:
  VIX < 15               → +VIX_LOW_BONUS          (default +5)
  VIX 20–25              → +VIX_MODERATE_PENALTY    (default -10)
  VIX > 25               → +VIX_HIGH_PENALTY        (default -20)
  US Futures up > 0.5%   → +US_FUTURES_UP_BONUS     (default +5)
  US Futures down > 0.5% → +US_FUTURES_DOWN_PENALTY (default -10)
  US Futures down > 1.0% → +US_FUTURES_CRASH_PENALTY (default -15)
  Headlines confirm signal → +SENTIMENT_CONFIRM_BONUS (default +3)
  Headlines oppose signal  → +SENTIMENT_CONTRADICT_PENALTY (default -5)

Results are cached for SENTIMENT_CACHE_SECONDS (default 300s).

Usage:
    from core.news_sentiment import NewsSentimentEngine
    engine = NewsSentimentEngine()
    result = engine.get_score_adjustment(signal_direction="BULLISH")
    # result["total_adjustment"] → int to add to confidence score
"""

import time
import logging
import xml.etree.ElementTree as ET
from typing import Optional

import requests

from config.config import (
    NSE_INDEX_URL,
    NSE_API_HEADERS,
    NSE_BASE_URL,
    VIX_LOW_THRESHOLD,
    VIX_MODERATE_THRESHOLD,
    VIX_HIGH_THRESHOLD,
    VIX_LOW_BONUS,
    VIX_MODERATE_PENALTY,
    VIX_HIGH_PENALTY,
    US_FUTURES_STRONG_UP_PCT,
    US_FUTURES_STRONG_DOWN_PCT,
    US_FUTURES_CRASH_PCT,
    US_FUTURES_UP_BONUS,
    US_FUTURES_DOWN_PENALTY,
    US_FUTURES_CRASH_PENALTY,
    US_FUTURES_TICKER,
    SENTIMENT_CONFIRM_BONUS,
    SENTIMENT_CONTRADICT_PENALTY,
    SENTIMENT_CACHE_SECONDS,
    RSS_FEEDS,
    GOOGLE_NEWS_NIFTY_RSS,
    SENTIMENT_BULLISH_KEYWORDS,
    SENTIMENT_BEARISH_KEYWORDS,
)

logger = logging.getLogger(__name__)

# Signal direction constants (match multi_timeframe.py)
DIR_BULLISH  = "BULLISH"
DIR_BEARISH  = "BEARISH"
DIR_SIDEWAYS = "SIDEWAYS"

# =========================
# NEWS SENTIMENT ENGINE
# =========================

class NewsSentimentEngine:
    """
    Aggregates VIX, US Futures, RSS, and Google News into a single
    confidence score adjustment integer.

    All source results cached in memory for SENTIMENT_CACHE_SECONDS.
    Individual sources are fetched in isolation — failure of one source
    does not block the rest.
    """

    def __init__(self):
        self._cache: Optional[dict] = None
        self._cache_ts: float       = 0.0
        self._session               = requests.Session()
        self._session.headers.update(NSE_API_HEADERS)
        self._nse_session_ready     = False

    # =========================
    # NSE SESSION
    # =========================

    def _init_nse_session(self) -> bool:
        if self._nse_session_ready:
            return True
        try:
            r = self._session.get(NSE_BASE_URL, timeout=10)
            if r.status_code == 200:
                self._nse_session_ready = True
                return True
        except Exception as e:
            logger.warning("[SENTIMENT][VIX] NSE session init failed: %s", e)
        return False

    # =========================
    # SOURCE 1 — INDIA VIX
    # =========================

    def _fetch_india_vix(self) -> Optional[float]:
        """
        Fetch India VIX from NSE allIndices API.
        The endpoint returns a list of indices — we find INDIA VIX by symbol.
        """
        try:
            self._init_nse_session()
            r = self._session.get(NSE_INDEX_URL, timeout=10)
            if r.status_code != 200:
                logger.warning("[SENTIMENT][VIX] NSE API returned %d", r.status_code)
                return None
            data    = r.json()
            indices = data.get("data", [])
            for idx in indices:
                if "VIX" in idx.get("indexSymbol", "").upper():
                    vix = float(idx.get("last", 0))
                    logger.info("[SENTIMENT][VIX] India VIX = %.2f", vix)
                    return vix
            logger.warning("[SENTIMENT][VIX] VIX not found in NSE index list")
            return None
        except Exception as e:
            logger.warning("[SENTIMENT][VIX] Fetch failed: %s", e)
            return None

    def _score_vix(self, vix: Optional[float]) -> tuple[int, str]:
        """Returns (score_adjustment, label)."""
        if vix is None:
            return 0, "VIX: N/A"
        if vix > VIX_HIGH_THRESHOLD:
            return VIX_HIGH_PENALTY, f"VIX={vix:.1f} [HIGH >25 → {VIX_HIGH_PENALTY}]"
        if vix > VIX_MODERATE_THRESHOLD:
            return VIX_MODERATE_PENALTY, f"VIX={vix:.1f} [ELEVATED 20-25 → {VIX_MODERATE_PENALTY}]"
        if vix < VIX_LOW_THRESHOLD:
            return VIX_LOW_BONUS, f"VIX={vix:.1f} [CALM <15 → +{VIX_LOW_BONUS}]"
        return 0, f"VIX={vix:.1f} [NORMAL 15-20 → 0]"

    # =========================
    # SOURCE 2 — US FUTURES
    # =========================

    def _fetch_us_futures_pct(self) -> Optional[float]:
        """
        Fetch S&P 500 E-mini futures (ES=F) daily percentage change via yfinance.
        Returns the percentage change as a float (e.g. -0.82 = down 0.82%).
        Falls back to a Yahoo Finance direct API call if yfinance is not installed.
        """
        # --- Try yfinance ---
        try:
            import yfinance as yf
            ticker = yf.Ticker(US_FUTURES_TICKER)
            info   = ticker.fast_info
            pct    = getattr(info, "last_price", None)
            # yfinance fast_info doesn't give pct_change directly
            # Use history instead
            hist = ticker.history(period="2d", interval="1d")
            if len(hist) >= 2:
                prev  = hist["Close"].iloc[-2]
                curr  = hist["Close"].iloc[-1]
                pct_chg = ((curr - prev) / prev) * 100
                logger.info("[SENTIMENT][US] ES=F change = %.2f%%", pct_chg)
                return round(pct_chg, 2)
            logger.warning("[SENTIMENT][US] yfinance returned < 2 rows")
            return None
        except ImportError:
            logger.debug("[SENTIMENT][US] yfinance not installed — trying Yahoo JSON API")
        except Exception as e:
            logger.warning("[SENTIMENT][US] yfinance error: %s", e)

        # --- Fallback: Yahoo Finance v8 JSON API ---
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{US_FUTURES_TICKER}"
            params = {"interval": "1d", "range": "2d"}
            headers = {"User-Agent": "Mozilla/5.0"}
            r = requests.get(url, params=params, headers=headers, timeout=10)
            if r.status_code != 200:
                logger.warning("[SENTIMENT][US] Yahoo API returned %d", r.status_code)
                return None
            result = r.json()["chart"]["result"][0]
            closes = result["indicators"]["quote"][0]["close"]
            closes = [c for c in closes if c is not None]
            if len(closes) < 2:
                return None
            pct_chg = ((closes[-1] - closes[-2]) / closes[-2]) * 100
            logger.info("[SENTIMENT][US] ES=F change = %.2f%% (Yahoo JSON)", pct_chg)
            return round(pct_chg, 2)
        except Exception as e:
            logger.warning("[SENTIMENT][US] Yahoo JSON fallback failed: %s", e)
            return None

    def _score_us_futures(self, pct: Optional[float]) -> tuple[int, str]:
        """Returns (score_adjustment, label)."""
        if pct is None:
            return 0, "US Futures: N/A"
        if pct <= US_FUTURES_CRASH_PCT:
            return US_FUTURES_CRASH_PENALTY, f"ES=F {pct:+.2f}% [CRASH → {US_FUTURES_CRASH_PENALTY}]"
        if pct <= US_FUTURES_STRONG_DOWN_PCT:
            return US_FUTURES_DOWN_PENALTY, f"ES=F {pct:+.2f}% [DOWN → {US_FUTURES_DOWN_PENALTY}]"
        if pct >= US_FUTURES_STRONG_UP_PCT:
            return US_FUTURES_UP_BONUS, f"ES=F {pct:+.2f}% [UP → +{US_FUTURES_UP_BONUS}]"
        return 0, f"ES=F {pct:+.2f}% [FLAT → 0]"

    # =========================
    # SOURCE 3 — RSS FEEDS
    # =========================

    def _fetch_rss_headlines(self) -> list[str]:
        """
        Fetch headlines from ET Markets, Moneycontrol, CNBCTV18.
        Returns a flat list of headline strings.
        Silently skips any feed that fails.
        """
        headlines = []
        session   = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0"})

        for name, url in RSS_FEEDS.items():
            try:
                r = session.get(url, timeout=8)
                if r.status_code != 200:
                    logger.warning("[SENTIMENT][RSS] %s returned %d", name, r.status_code)
                    continue
                root = ET.fromstring(r.content)
                items = root.findall(".//item")
                for item in items[:10]:  # Limit to 10 items per feed
                    title = item.findtext("title", default="")
                    if title:
                        headlines.append(title.strip())
                logger.debug("[SENTIMENT][RSS] %s: %d headlines", name, len(items))
            except Exception as e:
                logger.warning("[SENTIMENT][RSS] %s failed: %s", name, e)

        logger.info("[SENTIMENT][RSS] Total headlines collected: %d", len(headlines))
        return headlines

    # =========================
    # SOURCE 4 — GOOGLE NEWS
    # =========================

    def _fetch_google_news_headlines(self) -> list[str]:
        """
        Fetch NIFTY-related headlines from Google News RSS.
        Returns a list of headline strings.
        """
        headlines = []
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            r = requests.get(GOOGLE_NEWS_NIFTY_RSS, headers=headers, timeout=8)
            if r.status_code != 200:
                logger.warning("[SENTIMENT][GNEWS] Returned %d", r.status_code)
                return []
            root  = ET.fromstring(r.content)
            items = root.findall(".//item")
            for item in items[:15]:
                title = item.findtext("title", default="")
                if title:
                    headlines.append(title.strip())
            logger.info("[SENTIMENT][GNEWS] %d headlines fetched", len(headlines))
        except Exception as e:
            logger.warning("[SENTIMENT][GNEWS] Fetch failed: %s", e)
        return headlines

    # =========================
    # SENTIMENT SCORING
    # =========================

    def _score_headlines(
        self,
        headlines: list[str],
        signal_direction: str,
    ) -> tuple[int, str, int, int]:
        """
        Keyword-match headlines against bullish/bearish word lists.
        Returns:
            (score_adjustment, label, bullish_count, bearish_count)
        """
        bullish_count = 0
        bearish_count = 0

        for headline in headlines:
            h = headline.lower()
            for kw in SENTIMENT_BULLISH_KEYWORDS:
                if kw in h:
                    bullish_count += 1
                    break
            for kw in SENTIMENT_BEARISH_KEYWORDS:
                if kw in h:
                    bearish_count += 1
                    break

        total = bullish_count + bearish_count
        if total == 0:
            return 0, "Sentiment: NEUTRAL (no keywords)", bullish_count, bearish_count

        bull_pct = bullish_count / total
        bear_pct = bearish_count / total

        # Determine overall market mood
        if bull_pct >= 0.60:
            mood = "BULLISH"
        elif bear_pct >= 0.60:
            mood = "BEARISH"
        else:
            mood = "NEUTRAL"

        logger.info(
            "[SENTIMENT][HEADLINES] bull=%d bear=%d mood=%s signal=%s",
            bullish_count, bearish_count, mood, signal_direction
        )

        if mood == "NEUTRAL":
            return 0, f"Sentiment: NEUTRAL ({bullish_count}B/{bearish_count}b)", bullish_count, bearish_count

        # Check confirm / contradict
        confirms = (
            (mood == "BULLISH" and signal_direction == DIR_BULLISH) or
            (mood == "BEARISH" and signal_direction == DIR_BEARISH)
        )

        if confirms:
            adj   = SENTIMENT_CONFIRM_BONUS
            label = f"Sentiment: {mood} confirms signal → +{adj}"
        else:
            adj   = SENTIMENT_CONTRADICT_PENALTY
            label = f"Sentiment: {mood} contradicts signal → {adj}"

        return adj, label, bullish_count, bearish_count

    # =========================
    # PUBLIC API
    # =========================

    def get_score_adjustment(self, signal_direction: str) -> dict:
        """
        Main entry point. Fetches all sources, aggregates adjustments.

        Returns a dict with:
            total_adjustment   int   — sum of all adjustments (add to confidence)
            vix                float or None
            vix_adjustment     int
            us_futures_pct     float or None
            futures_adjustment int
            sentiment          str   — 'BULLISH' / 'BEARISH' / 'NEUTRAL'
            sentiment_adjustment int
            bullish_headlines  int
            bearish_headlines  int
            total_headlines    int
            labels             list[str]   — human-readable per-source labels
            valid              bool
            cached             bool
        """
        now = time.time()

        # --- Cache check (direction-independent — recompute sent adj only) ---
        if self._cache and (now - self._cache_ts) < SENTIMENT_CACHE_SECONDS:
            logger.debug("[SENTIMENT] Using cached data (age=%.0fs)", now - self._cache_ts)
            cached = dict(self._cache)
            # Recompute sentiment adjustment for current signal direction
            headlines = cached.get("_raw_headlines", [])
            sent_adj, sent_label, bull, bear = self._score_headlines(headlines, signal_direction)
            cached["sentiment_adjustment"] = sent_adj
            cached["bullish_headlines"]    = bull
            cached["bearish_headlines"]    = bear
            cached["total_headlines"]      = bull + bear
            labels = [cached.get("_vix_label", ""), cached.get("_futures_label", ""), sent_label]
            cached["labels"]           = [l for l in labels if l]
            cached["total_adjustment"] = (
                cached.get("vix_adjustment", 0) +
                cached.get("futures_adjustment", 0) +
                sent_adj
            )
            cached["cached"] = True
            return cached

        logger.info("[SENTIMENT] Fetching all sentiment sources...")
        labels = []

        # --- VIX ---
        vix              = self._fetch_india_vix()
        vix_adj, vl      = self._score_vix(vix)
        labels.append(vl)

        # --- US Futures ---
        us_pct               = self._fetch_us_futures_pct()
        futures_adj, fl      = self._score_us_futures(us_pct)
        labels.append(fl)

        # --- Headlines (RSS + Google News) ---
        headlines     = self._fetch_rss_headlines() + self._fetch_google_news_headlines()
        sent_adj, sl, bull, bear = self._score_headlines(headlines, signal_direction)
        labels.append(sl)

        # Determine overall mood string
        total_hl = bull + bear
        if total_hl == 0:
            mood = "NEUTRAL"
        elif bull / total_hl >= 0.60:
            mood = "BULLISH"
        elif bear / total_hl >= 0.60:
            mood = "BEARISH"
        else:
            mood = "NEUTRAL"

        total_adj = vix_adj + futures_adj + sent_adj

        logger.info(
            "[SENTIMENT] VIX=%s(%+d)  Futures=%s(%+d)  Sent=%s(%+d)  TOTAL=%+d",
            f"{vix:.1f}" if vix else "N/A", vix_adj,
            f"{us_pct:+.2f}%" if us_pct is not None else "N/A", futures_adj,
            mood, sent_adj,
            total_adj,
        )

        result = {
            "total_adjustment":    total_adj,
            "vix":                 vix,
            "vix_adjustment":      vix_adj,
            "us_futures_pct":      us_pct,
            "futures_adjustment":  futures_adj,
            "sentiment":           mood,
            "sentiment_adjustment": sent_adj,
            "bullish_headlines":   bull,
            "bearish_headlines":   bear,
            "total_headlines":     total_hl,
            "labels":              [l for l in labels if l],
            "valid":               True,
            "cached":              False,
            # Internal cache fields
            "_raw_headlines":      headlines,
            "_vix_label":          vl,
            "_futures_label":      fl,
        }

        self._cache    = result.copy()
        self._cache_ts = now

        return result


# =========================
# STANDALONE TEST
# =========================

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s | %(name)s | %(message)s")

    direction = sys.argv[1] if len(sys.argv) > 1 else "BULLISH"
    print(f"\nTesting News Sentiment Engine | direction={direction}")

    engine = NewsSentimentEngine()
    result = engine.get_score_adjustment(direction)

    print("\n--- Sentiment Result ---")
    for k, v in result.items():
        if k.startswith("_"):
            continue
        if k == "labels":
            print(f"  {'labels':<25}:")
            for l in v:
                print(f"      {l}")
        else:
            print(f"  {k:<25}: {v}")
