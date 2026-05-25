"""
core/oi_analysis.py
===================
Open Interest (OI) Analysis Engine for NIFTY options.

What it does:
  - Fetches the NIFTY option chain from NSE's unofficial JSON API
  - Falls back to HTML scraping if the API call fails
  - Computes:
      PCR   — Put-Call Ratio (total OI in the scanned strike window)
      Max Pain — strike price where total OI loss across all strikes is minimised
      ATM bias  — CE OI vs PE OI at the at-the-money strike
  - Returns a confidence score adjustment (-20 to +8 points) based on
    whether OI confirms or contradicts the current trade signal direction

Score adjustment rules:
  PCR confirms signal  → +OI_SCORE_CONFIRM   (default +8)
  PCR contradicts      → +OI_SCORE_CONTRADICT (default -15)
  Max pain gravitational pull risk → OI_MAX_PAIN_GRAVITY_PENALTY (default -5)

Results are cached for OI_CACHE_SECONDS (default 300s = 5 min) to avoid
hammering NSE on every 5-minute scan cycle.

Usage:
    from core.oi_analysis import OIAnalysis
    oi = OIAnalysis()
    result = oi.get_score_adjustment(current_price=24100.0, signal_direction="BULLISH")
    # result["score_adjustment"] → int to add to confidence score
"""

import time
import logging
import requests
from typing import Optional

from config.config import (
    NSE_BASE_URL,
    NSE_OPTION_CHAIN_URL,
    NSE_API_HEADERS,
    NIFTY_STRIKE_INTERVAL,
    OI_PCR_BULLISH_THRESHOLD,
    OI_PCR_BEARISH_THRESHOLD,
    OI_ATM_RANGE_STRIKES,
    OI_SCORE_CONFIRM,
    OI_SCORE_CONTRADICT,
    OI_MAX_PAIN_GRAVITY_POINTS,
    OI_MAX_PAIN_GRAVITY_PENALTY,
    OI_CACHE_SECONDS,
)

logger = logging.getLogger(__name__)

# Signal direction constants (match multi_timeframe.py)
DIR_BULLISH  = "BULLISH"
DIR_BEARISH  = "BEARISH"
DIR_SIDEWAYS = "SIDEWAYS"


# =========================
# OI ANALYSIS ENGINE
# =========================

class OIAnalysis:
    """
    Fetches NIFTY option chain from NSE and computes OI-based
    confidence score adjustments.

    Thread safety: single-threaded use only (no locks).
    Cache: results stored in memory for OI_CACHE_SECONDS seconds.
    """

    def __init__(self):
        self._cache: Optional[dict] = None
        self._cache_ts: float       = 0.0
        self._session               = requests.Session()
        self._session.headers.update(NSE_API_HEADERS)
        self._session_initialised   = False

    # =========================
    # SESSION INIT
    # NSE API requires a valid session cookie obtained by hitting the
    # main page first. Without this the API returns 401 or garbled data.
    # =========================

    def _init_nse_session(self) -> bool:
        """Hit NSE home page to acquire session cookie."""
        if self._session_initialised:
            return True
        try:
            logger.debug("[OI] Initialising NSE session (cookie handshake)...")
            r = self._session.get(NSE_BASE_URL, timeout=10)
            if r.status_code == 200:
                self._session_initialised = True
                logger.debug("[OI] NSE session ready.")
                return True
            logger.warning("[OI] NSE session init returned %d", r.status_code)
        except Exception as e:
            logger.warning("[OI] NSE session init failed: %s", e)
        return False

    # =========================
    # OPTION CHAIN FETCH
    # =========================

    def _fetch_option_chain_api(self) -> Optional[dict]:
        """
        Fetch NIFTY option chain via NSE unofficial JSON API.
        Returns the raw JSON dict or None on failure.
        """
        if not self._init_nse_session():
            return None
        try:
            r = self._session.get(NSE_OPTION_CHAIN_URL, timeout=12)
            if r.status_code != 200:
                logger.warning("[OI] API returned HTTP %d", r.status_code)
                return None
            data = r.json()
            if "records" not in data or "data" not in data["records"]:
                logger.warning("[OI] Unexpected API response structure")
                return None
            logger.info("[OI] Option chain fetched via API (%d records)",
                        len(data["records"]["data"]))
            return data
        except Exception as e:
            logger.warning("[OI] API fetch failed: %s", e)
            return None

    def _fetch_option_chain_scrape(self) -> Optional[dict]:
        """
        Fallback: scrape NSE option chain page and parse embedded JSON.
        NSE embeds __NEXT_DATA__ or a JSON blob in the HTML — this
        extracts it with a simple regex approach.
        """
        import re
        try:
            logger.info("[OI] Falling back to page scrape...")
            # Re-init session so cookie is fresh
            self._session_initialised = False
            self._init_nse_session()
            url = "https://www.nseindia.com/option-chain"
            r   = self._session.get(url, timeout=15)
            if r.status_code != 200:
                logger.warning("[OI] Scrape page returned HTTP %d", r.status_code)
                return None
            # NSE sometimes embeds the full JSON in a <script> tag
            match = re.search(
                r'window\.__NEXT_DATA__\s*=\s*(\{.*?\});\s*</script>',
                r.text, re.DOTALL
            )
            if not match:
                logger.warning("[OI] Could not find embedded JSON in page")
                return None
            import json
            raw = json.loads(match.group(1))
            # Path inside __NEXT_DATA__ varies; look for option chain records
            records = (raw.get("props", {})
                          .get("pageProps", {})
                          .get("data", {})
                          .get("records"))
            if records and "data" in records:
                logger.info("[OI] Option chain scraped (%d records)",
                            len(records["data"]))
                return {"records": records}
            logger.warning("[OI] Scraped JSON has no usable records")
            return None
        except Exception as e:
            logger.warning("[OI] Scrape fallback failed: %s", e)
            return None

    def _fetch_option_chain(self) -> Optional[dict]:
        """Try API first, then scraping fallback."""
        data = self._fetch_option_chain_api()
        if data is None:
            data = self._fetch_option_chain_scrape()
        return data

    # =========================
    # OI COMPUTATIONS
    # =========================

    def _atm_strike(self, current_price: float) -> int:
        """Round current_price to nearest NIFTY_STRIKE_INTERVAL."""
        interval = NIFTY_STRIKE_INTERVAL
        return int(round(current_price / interval) * interval)

    def _compute_pcr(self, records: list, atm: int) -> Optional[float]:
        """
        Compute Put-Call Ratio for the OI_ATM_RANGE_STRIKES window
        around the ATM strike.

        PCR = sum(PE OI) / sum(CE OI) in that window.
        Returns None if data is insufficient.
        """
        n        = OI_ATM_RANGE_STRIKES
        interval = NIFTY_STRIKE_INTERVAL
        strikes  = {atm + i * interval for i in range(-n, n + 1)}

        total_ce_oi = 0
        total_pe_oi = 0

        for record in records:
            strike = record.get("strikePrice")
            if strike not in strikes:
                continue
            ce_data = record.get("CE", {})
            pe_data = record.get("PE", {})
            total_ce_oi += ce_data.get("openInterest", 0) or 0
            total_pe_oi += pe_data.get("openInterest", 0) or 0

        if total_ce_oi == 0:
            logger.warning("[OI] CE OI is zero — cannot compute PCR")
            return None

        pcr = total_pe_oi / total_ce_oi
        logger.info("[OI] PCR=%.3f  CE_OI=%d  PE_OI=%d  (±%d strikes of ATM %d)",
                    pcr, total_ce_oi, total_pe_oi, n, atm)
        return pcr

    def _compute_max_pain(self, records: list) -> Optional[float]:
        """
        Compute max pain strike — the strike where total OI loss
        (for all option holders) is minimised if expiry closes there.

        Algorithm:
          For each candidate strike S:
            loss_CE = sum over all strikes K of max(0, K - S) * CE_OI(K)
            loss_PE = sum over all strikes K of max(0, S - K) * PE_OI(K)
            total_loss(S) = loss_CE + loss_PE
          Max pain = argmin(total_loss)
        """
        try:
            # Build strike → OI maps
            ce_oi = {}
            pe_oi = {}
            for record in records:
                k = record.get("strikePrice")
                if k is None:
                    continue
                ce_oi[k] = record.get("CE", {}).get("openInterest", 0) or 0
                pe_oi[k] = record.get("PE", {}).get("openInterest", 0) or 0

            strikes = sorted(set(ce_oi) | set(pe_oi))
            if len(strikes) < 5:
                return None

            min_loss   = float("inf")
            max_pain_k = None

            for s in strikes:
                loss = 0
                for k in strikes:
                    loss += max(0, k - s) * ce_oi.get(k, 0)   # CE writers lose
                    loss += max(0, s - k) * pe_oi.get(k, 0)   # PE writers lose
                if loss < min_loss:
                    min_loss   = loss
                    max_pain_k = s

            logger.info("[OI] Max pain strike: %s", max_pain_k)
            return float(max_pain_k) if max_pain_k is not None else None

        except Exception as e:
            logger.warning("[OI] Max pain computation failed: %s", e)
            return None

    def _compute_atm_bias(self, records: list, atm: int) -> str:
        """
        Compare CE OI vs PE OI at the exact ATM strike.
        Returns 'BULLISH', 'BEARISH', or 'NEUTRAL'.
        """
        for record in records:
            if record.get("strikePrice") == atm:
                ce = record.get("CE", {}).get("openInterest", 0) or 0
                pe = record.get("PE", {}).get("openInterest", 0) or 0
                if ce == 0 and pe == 0:
                    return "NEUTRAL"
                if pe > ce * 1.2:
                    return "BULLISH"   # Heavy PE OI at ATM = support = bullish
                if ce > pe * 1.2:
                    return "BEARISH"   # Heavy CE OI at ATM = resistance = bearish
                return "NEUTRAL"
        return "NEUTRAL"

    # =========================
    # SCORE ADJUSTMENT
    # =========================

    def _score_from_pcr(self, pcr: float, signal_direction: str) -> int:
        """
        Translate PCR and signal direction into a confidence adjustment.

        PCR > 1.3  → bullish OI sentiment (heavy PE = market expects support)
        PCR < 0.7  → bearish OI sentiment (heavy CE = market expects resistance)
        0.7–1.3    → neutral

        Confirm  = OI and signal agree  → +OI_SCORE_CONFIRM
        Contradict = OI and signal oppose → OI_SCORE_CONTRADICT
        Neutral  → 0
        """
        oi_bias = "NEUTRAL"
        if pcr >= OI_PCR_BULLISH_THRESHOLD:
            oi_bias = "BULLISH"
        elif pcr <= OI_PCR_BEARISH_THRESHOLD:
            oi_bias = "BEARISH"

        if oi_bias == "NEUTRAL":
            logger.info("[OI] PCR neutral (%.3f) — no adjustment", pcr)
            return 0

        confirms = (
            (oi_bias == "BULLISH" and signal_direction == DIR_BULLISH) or
            (oi_bias == "BEARISH" and signal_direction == DIR_BEARISH)
        )

        if confirms:
            logger.info("[OI] PCR confirms signal (%s) → +%d", oi_bias, OI_SCORE_CONFIRM)
            return OI_SCORE_CONFIRM
        else:
            logger.info("[OI] PCR contradicts signal (%s vs %s) → %d",
                        oi_bias, signal_direction, OI_SCORE_CONTRADICT)
            return OI_SCORE_CONTRADICT

    def _score_from_max_pain(self, max_pain: Optional[float],
                             current_price: float) -> int:
        """
        If current price is far from max pain, the market has a
        gravitational pull back toward max pain at expiry.
        Apply a small penalty when this distance is large.
        """
        if max_pain is None:
            return 0
        distance = abs(current_price - max_pain)
        if distance > OI_MAX_PAIN_GRAVITY_POINTS:
            logger.info("[OI] Max pain distance %.0f pts > %.0f threshold → %d",
                        distance, OI_MAX_PAIN_GRAVITY_POINTS,
                        OI_MAX_PAIN_GRAVITY_PENALTY)
            return OI_MAX_PAIN_GRAVITY_PENALTY
        return 0

    # =========================
    # OPTION PREMIUM FETCH
    # =========================

    def get_option_premium(self, strike: int, direction: str) -> Optional[float]:
        """
        Fetch the last traded price (LTP/lastPrice) for a specific NIFTY option.

        Args:
            strike:    Strike price (e.g. 23800)
            direction: "CE" or "PE"

        Returns:
            Premium in NIFTY points (e.g. 145.0), or None if unavailable.
            Total cost of 1 lot = premium × NIFTY_LOT_SIZE
        """
        if not self._init_nse_session():
            return None
        try:
            r = self._session.get(NSE_OPTION_CHAIN_URL, timeout=12)
            if r.status_code != 200:
                logger.warning("[OI] get_option_premium: HTTP %d", r.status_code)
                return None
            records = r.json().get("records", {}).get("data", [])
            for rec in records:
                if rec.get("strikePrice") == strike:
                    opt = rec.get(direction.upper(), {})
                    ltp = opt.get("lastPrice") or opt.get("ltp") or opt.get("close")
                    if ltp and float(ltp) > 0:
                        logger.info("[OI] Premium | %d %s LTP=%.2f", strike, direction, float(ltp))
                        return float(ltp)
            logger.warning("[OI] Strike %d %s not found in option chain", strike, direction)
            return None
        except Exception as e:
            logger.warning("[OI] get_option_premium failed: %s", e)
            return None

    # =========================
    # PUBLIC API
    # =========================

    def get_score_adjustment(
        self,
        current_price: float,
        signal_direction: str,
    ) -> dict:
        """
        Main entry point. Returns a dict with:
            score_adjustment  int   — points to add to confidence score
            pcr               float or None
            max_pain          float or None
            atm_bias          str   — 'BULLISH' / 'BEARISH' / 'NEUTRAL'
            valid             bool
            error             str or None
            source            str   — 'api', 'scrape', or 'cache'

        Always returns a valid dict — on failure, score_adjustment = 0.
        """
        now = time.time()

        # --- Cache check ---
        if self._cache and (now - self._cache_ts) < OI_CACHE_SECONDS:
            logger.debug("[OI] Using cached OI data (age=%.0fs)", now - self._cache_ts)
            cached = dict(self._cache)
            # Recompute score adjustment for the current signal direction
            pcr      = cached.get("pcr")
            max_pain = cached.get("max_pain")
            adj      = 0
            if pcr is not None:
                adj += self._score_from_pcr(pcr, signal_direction)
            adj += self._score_from_max_pain(max_pain, current_price)
            cached["score_adjustment"] = adj
            cached["source"]           = "cache"
            return cached

        # --- Fetch fresh data ---
        logger.info("[OI] Fetching fresh option chain data...")
        data = self._fetch_option_chain()

        if data is None:
            logger.warning("[OI] Option chain unavailable — returning 0 adjustment")
            return {
                "score_adjustment": 0,
                "pcr":              None,
                "max_pain":         None,
                "atm_bias":         "NEUTRAL",
                "valid":            False,
                "error":            "Option chain fetch failed",
                "source":           "none",
            }

        records = data["records"]["data"]
        atm     = self._atm_strike(current_price)

        # Compute metrics
        pcr      = self._compute_pcr(records, atm)
        max_pain = self._compute_max_pain(records)
        atm_bias = self._compute_atm_bias(records, atm)

        # Score adjustment
        adj = 0
        if pcr is not None:
            adj += self._score_from_pcr(pcr, signal_direction)
        adj += self._score_from_max_pain(max_pain, current_price)

        result = {
            "score_adjustment": adj,
            "pcr":              pcr,
            "max_pain":         max_pain,
            "atm_bias":         atm_bias,
            "valid":            True,
            "error":            None,
            "source":           "api",
        }

        # Cache (store without score_adjustment — recomputed per direction)
        self._cache    = result.copy()
        self._cache_ts = now

        logger.info(
            "[OI] Result | PCR=%.3f | MaxPain=%s | ATMbias=%s | adjustment=%+d",
            pcr if pcr else 0,
            f"{max_pain:.0f}" if max_pain else "N/A",
            atm_bias,
            adj,
        )
        return result


# =========================
# STANDALONE TEST
# =========================

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s | %(name)s | %(message)s")

    price = float(sys.argv[1]) if len(sys.argv) > 1 else 24100.0
    direction = sys.argv[2] if len(sys.argv) > 2 else "BULLISH"

    print(f"\nTesting OI Analysis | price={price} | direction={direction}")
    oi = OIAnalysis()
    result = oi.get_score_adjustment(price, direction)
    print("\n--- OI Result ---")
    for k, v in result.items():
        print(f"  {k:<20}: {v}")
