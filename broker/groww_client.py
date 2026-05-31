"""
broker/groww_client.py
======================
Raw HTTP wrapper around the Groww Broker API.

STATUS: STUB — all methods are fully structured and documented but HTTP calls
        are marked TODO. Fill them in when Groww API credentials arrive.

Authentication
--------------
Groww uses API Key + Access Token authentication.
Tokens are short-lived; call refresh_token() if a 401 is received.

Secrets
-------
Load from .env:
    GROWW_API_KEY=your_api_key_here
    GROWW_ACCESS_TOKEN=your_access_token_here

Never hardcode credentials in source.
"""

import logging
import time
from typing import Dict, Optional

import requests

from config.settings import (
    GROWW_API_BASE_URL,
    GROWW_API_KEY,
    GROWW_ACCESS_TOKEN,
    GROWW_API_TIMEOUT,
    GROWW_ORDER_RETRY_MAX,
    GROWW_EXCHANGE,
    GROWW_PRODUCT,
    GROWW_ORDER_TYPE,
    GROWW_VALIDITY,
    PAPER_TRADING_MODE,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TRANSACTION_BUY  = "BUY"
TRANSACTION_SELL = "SELL"

OPTION_CE = "CE"
OPTION_PE  = "PE"


class GrowwAPIError(Exception):
    """Raised when the Groww API returns an error response."""
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message     = message
        super().__init__(f"GrowwAPI {status_code}: {message}")


class GrowwClient:
    """
    Low-level Groww REST API client.

    All public methods return a dict on success and raise GrowwAPIError on failure.
    Methods are safe to call in paper trading mode — they will raise RuntimeError
    immediately if PAPER_TRADING_MODE is True, so order_manager.py should gate
    all calls behind the paper check before calling this class.
    """

    def __init__(self) -> None:
        self._session     = requests.Session()
        self._api_key     = GROWW_API_KEY
        self._access_token = GROWW_ACCESS_TOKEN
        self._base_url    = GROWW_API_BASE_URL.rstrip("/")
        self._connected   = False

        if PAPER_TRADING_MODE:
            logger.info("[GrowwClient] Initialised in PAPER mode — no HTTP calls will be made")
        else:
            if not self._api_key or not self._access_token:
                logger.critical(
                    "[GrowwClient] LIVE mode but GROWW_API_KEY or GROWW_ACCESS_TOKEN missing! "
                    "Check .env file."
                )

    # ==========================================================================
    # AUTHENTICATION
    # ==========================================================================

    def authenticate(self) -> bool:
        """
        Verify credentials and obtain/refresh access token.

        Returns True on success, False on failure.

        TODO: Replace stub with real Groww auth endpoint when credentials arrive.
              Expected endpoint: POST /v1/auth/token
              Body: {"api_key": ..., "secret_key": ...}
              Response: {"access_token": ..., "expires_in": 86400}
        """
        if PAPER_TRADING_MODE:
            logger.debug("[GrowwClient] authenticate() skipped — paper mode")
            self._connected = True
            return True

        # TODO: Implement real auth
        # try:
        #     resp = self._session.post(
        #         f"{self._base_url}/auth/token",
        #         json={"api_key": self._api_key, "secret_key": os.getenv("GROWW_SECRET_KEY","")},
        #         timeout=GROWW_API_TIMEOUT,
        #     )
        #     resp.raise_for_status()
        #     data = resp.json()
        #     self._access_token = data["access_token"]
        #     self._session.headers.update({"Authorization": f"Bearer {self._access_token}"})
        #     self._connected = True
        #     logger.info("[GrowwClient] Authenticated successfully")
        #     return True
        # except Exception as exc:
        #     logger.error("[GrowwClient] Authentication failed: %s", exc)
        #     return False

        logger.warning("[GrowwClient] authenticate() — TODO: implement real auth")
        self._connected = True  # Remove this when real auth is wired in
        return True

    def refresh_token(self) -> bool:
        """
        Refresh an expired access token.

        TODO: Implement using Groww refresh endpoint when documented.
        """
        logger.info("[GrowwClient] refresh_token() called")
        return self.authenticate()

    # ==========================================================================
    # ORDER PLACEMENT
    # ==========================================================================

    def place_order(
        self,
        symbol:           str,   # e.g. "NIFTY"
        strike:           int,   # e.g. 23500
        option_type:      str,   # "CE" or "PE"
        expiry:           str,   # e.g. "29MAY2026" — nearest weekly expiry
        qty:              int,   # number of lots × lot_size (e.g. 1 lot = 75 qty)
        transaction_type: str,   # "BUY" or "SELL"
        order_type:       str = GROWW_ORDER_TYPE,   # "MARKET" or "LIMIT"
        price:            float = 0.0,              # only for LIMIT orders
        trigger_price:    float = 0.0,              # only for SL orders
        product:          str   = GROWW_PRODUCT,
        validity:         str   = GROWW_VALIDITY,
        tag:              str   = "TradingBot",
    ) -> Dict:
        """
        Place a new F&O option order on Groww.

        Returns
        -------
        {
            "order_id":     str,   # Groww order ID
            "status":       str,   # "PENDING" | "COMPLETE" | "REJECTED"
            "message":      str,
            "symbol":       str,
            "strike":       int,
            "option_type":  str,
            "qty":          int,
            "transaction":  str,
        }

        Raises
        ------
        RuntimeError    if called in paper trading mode (shouldn't happen — gate in order_manager)
        GrowwAPIError   if Groww returns a non-2xx response

        TODO: Fill in real HTTP call.
              Expected endpoint: POST /v1/orders
              Body: {
                  "trading_symbol": "NIFTY26MAY23500CE",  # build with _build_symbol()
                  "exchange":        "NSE",
                  "segment":         "FNO",
                  "transaction_type": "BUY",
                  "order_type":      "MARKET",
                  "product":         "INTRADAY",
                  "quantity":        75,
                  "price":           0,
                  "trigger_price":   0,
                  "validity":        "DAY",
                  "tag":             "TradingBot",
              }
        """
        if PAPER_TRADING_MODE:
            raise RuntimeError("place_order() called in paper trading mode — this is a bug")

        trading_symbol = self._build_trading_symbol(symbol, strike, option_type, expiry)
        payload = {
            "trading_symbol":   trading_symbol,
            "exchange":         GROWW_EXCHANGE,
            "segment":          "FNO",
            "transaction_type": transaction_type,
            "order_type":       order_type,
            "product":          product,
            "quantity":         qty,
            "price":            price,
            "trigger_price":    trigger_price,
            "validity":         validity,
            "tag":              tag,
        }

        logger.info(
            "[GrowwClient] place_order | %s %s %s | qty=%d | type=%s",
            transaction_type, trading_symbol, order_type, qty, product,
        )

        # TODO: Uncomment and adjust when real API is wired in
        # return self._post("/orders", payload)

        # Stub response (remove when real API is wired in)
        logger.warning("[GrowwClient] place_order() — TODO: implement real HTTP call")
        return {
            "order_id":    f"STUB_{int(time.time())}",
            "status":      "PENDING",
            "message":     "STUB — real API not yet wired",
            "symbol":      symbol,
            "strike":      strike,
            "option_type": option_type,
            "qty":         qty,
            "transaction": transaction_type,
        }

    def cancel_order(self, order_id: str) -> Dict:
        """
        Cancel an open order by order_id.

        Returns {"order_id": ..., "status": "CANCELLED"} on success.

        TODO: Expected endpoint: DELETE /v1/orders/{order_id}
        """
        if PAPER_TRADING_MODE:
            raise RuntimeError("cancel_order() called in paper trading mode")

        logger.info("[GrowwClient] cancel_order | order_id=%s", order_id)

        # TODO: return self._delete(f"/orders/{order_id}")

        logger.warning("[GrowwClient] cancel_order() — TODO: implement real HTTP call")
        return {"order_id": order_id, "status": "CANCELLED", "message": "STUB"}

    def modify_order(
        self,
        order_id:  str,
        qty:       Optional[int]   = None,
        price:     Optional[float] = None,
        order_type: Optional[str]  = None,
    ) -> Dict:
        """
        Modify an open LIMIT order (price or quantity).

        TODO: Expected endpoint: PUT /v1/orders/{order_id}
        """
        if PAPER_TRADING_MODE:
            raise RuntimeError("modify_order() called in paper trading mode")

        logger.info("[GrowwClient] modify_order | order_id=%s", order_id)

        # TODO: return self._put(f"/orders/{order_id}", {...})

        logger.warning("[GrowwClient] modify_order() — TODO: implement real HTTP call")
        return {"order_id": order_id, "status": "MODIFIED", "message": "STUB"}

    # ==========================================================================
    # ORDER STATUS
    # ==========================================================================

    def get_order_status(self, order_id: str) -> Dict:
        """
        Fetch current status of an order.

        Returns
        -------
        {
            "order_id":     str,
            "status":       str,   # "PENDING" | "OPEN" | "COMPLETE" | "REJECTED" | "CANCELLED"
            "filled_qty":   int,
            "avg_price":    float,
            "message":      str,
        }

        TODO: Expected endpoint: GET /v1/orders/{order_id}
        """
        if PAPER_TRADING_MODE:
            raise RuntimeError("get_order_status() called in paper trading mode")

        # TODO: return self._get(f"/orders/{order_id}")

        logger.warning("[GrowwClient] get_order_status() — TODO: implement")
        return {
            "order_id":   order_id,
            "status":     "COMPLETE",
            "filled_qty": 75,
            "avg_price":  0.0,
            "message":    "STUB",
        }

    def get_order_book(self) -> list:
        """
        Fetch all orders for the current trading day.

        TODO: Expected endpoint: GET /v1/orders
        """
        if PAPER_TRADING_MODE:
            raise RuntimeError("get_order_book() called in paper trading mode")

        # TODO: return self._get("/orders")

        logger.warning("[GrowwClient] get_order_book() — TODO: implement")
        return []

    # ==========================================================================
    # POSITIONS & HOLDINGS
    # ==========================================================================

    def get_positions(self) -> list:
        """
        Fetch all open F&O positions.

        Returns list of position dicts:
        {
            "trading_symbol": str,
            "qty":            int,   # positive=long, negative=short
            "avg_price":      float,
            "ltp":            float,
            "pnl":            float,
            "product":        str,
        }

        TODO: Expected endpoint: GET /v1/positions
        """
        if PAPER_TRADING_MODE:
            raise RuntimeError("get_positions() called in paper trading mode")

        # TODO: return self._get("/positions")

        logger.warning("[GrowwClient] get_positions() — TODO: implement")
        return []

    def get_holdings(self) -> list:
        """
        Fetch equity holdings (not F&O). Included for completeness.

        TODO: Expected endpoint: GET /v1/holdings
        """
        if PAPER_TRADING_MODE:
            raise RuntimeError("get_holdings() called in paper trading mode")

        # TODO: return self._get("/holdings")

        logger.warning("[GrowwClient] get_holdings() — TODO: implement")
        return []

    def get_funds(self) -> Dict:
        """
        Fetch available margin / fund balance.

        Returns {"available_margin": float, "used_margin": float}

        TODO: Expected endpoint: GET /v1/funds
        """
        if PAPER_TRADING_MODE:
            raise RuntimeError("get_funds() called in paper trading mode")

        # TODO: return self._get("/funds")

        logger.warning("[GrowwClient] get_funds() — TODO: implement")
        return {"available_margin": 0.0, "used_margin": 0.0}

    # ==========================================================================
    # INTERNAL HELPERS
    # ==========================================================================

    def _build_trading_symbol(
        self,
        symbol:      str,  # "NIFTY"
        strike:      int,  # 23500
        option_type: str,  # "CE" / "PE"
        expiry:      str,  # "29MAY2026"
    ) -> str:
        """
        Build Groww trading symbol string.
        Format (Groww FNO): NIFTY26MAY23500CE

        TODO: Verify exact format with Groww API docs when credentials arrive.
              Some brokers use NIFTY26MAY2326500CE (zero-padded) — confirm.
        """
        # Parse expiry like "29MAY2026" → "26MAY26" (2-digit year, no day)
        # Example: strike=23500, expiry="29MAY2026", type="CE"
        # → "NIFTY26MAY23500CE"
        try:
            parts     = expiry.upper()          # "29MAY2026"
            month_str = parts[2:5]              # "MAY"
            year_str  = parts[7:9]              # "26"
            return f"{symbol.upper()}{year_str}{month_str}{strike}{option_type.upper()}"
        except Exception:
            # Fallback — return raw concatenation; fix format with actual API docs
            return f"{symbol}{expiry}{strike}{option_type}"

    def _get_headers(self) -> Dict:
        return {
            "Content-Type":  "application/json",
            "Accept":        "application/json",
            "X-API-Key":     self._api_key,
            "Authorization": f"Bearer {self._access_token}",
        }

    def _post(self, endpoint: str, payload: Dict) -> Dict:
        return self._request("POST", endpoint, json=payload)

    def _get(self, endpoint: str, params: Dict = None) -> Dict:
        return self._request("GET", endpoint, params=params)

    def _put(self, endpoint: str, payload: Dict) -> Dict:
        return self._request("PUT", endpoint, json=payload)

    def _delete(self, endpoint: str) -> Dict:
        return self._request("DELETE", endpoint)

    def _request(self, method: str, endpoint: str, **kwargs) -> Dict:
        """
        Execute an HTTP request with retry on transient failures (5xx / timeout).
        Raises GrowwAPIError on final failure.
        """
        url = f"{self._base_url}{endpoint}"
        last_exc = None

        for attempt in range(1, GROWW_ORDER_RETRY_MAX + 1):
            try:
                resp = self._session.request(
                    method,
                    url,
                    headers=self._get_headers(),
                    timeout=GROWW_API_TIMEOUT,
                    **kwargs,
                )

                if resp.status_code == 401:
                    logger.warning("[GrowwClient] 401 — refreshing token (attempt %d)", attempt)
                    self.refresh_token()
                    continue

                if resp.status_code >= 500:
                    logger.warning("[GrowwClient] %s error — retry %d/%d",
                                   resp.status_code, attempt, GROWW_ORDER_RETRY_MAX)
                    time.sleep(0.5 * attempt)
                    continue

                if not resp.ok:
                    raise GrowwAPIError(resp.status_code, resp.text[:200])

                return resp.json()

            except requests.Timeout as exc:
                logger.warning("[GrowwClient] Timeout on attempt %d/%d: %s",
                               attempt, GROWW_ORDER_RETRY_MAX, exc)
                last_exc = exc
                time.sleep(0.5 * attempt)

            except requests.RequestException as exc:
                logger.error("[GrowwClient] Request error: %s", exc)
                last_exc = exc
                break

        raise GrowwAPIError(0, f"All retries failed: {last_exc}")
