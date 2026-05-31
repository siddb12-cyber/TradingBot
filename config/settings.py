"""
config/settings.py
==================
Single source of truth for all TradingBot configuration.
Replaces config/config.py.

PAPER TRADING ONLY — PAPER_TRADING_MODE must remain True until
explicitly signed off for live deployment.

Secrets loaded from .env (BOT_TOKEN, CHAT_ID).
All strategy values are NIFTY INDEX POINTS — not percentages.
"""

import os
import logging
from pathlib import Path
from dotenv import load_dotenv

# =============================================================================
# BASE DIRECTORY
# Resolves to TradingBot/ regardless of where Python is called from.
# =============================================================================

BASE_DIR = Path(__file__).parent.parent.resolve()

# =============================================================================
# LOAD .env SECRETS
# =============================================================================

_env_path = BASE_DIR / ".env"
if not _env_path.exists():
    logging.warning(
        f"[SETTINGS] .env not found at {_env_path}. "
        "Copy .env.example → .env and fill BOT_TOKEN + CHAT_ID."
    )
load_dotenv(dotenv_path=_env_path)

# =============================================================================
# PAPER TRADING KILL-SWITCH
# True  = Groww order placement permanently blocked
# False = Live trading (requires explicit code change + sign-off)
# =============================================================================

PAPER_TRADING_MODE: bool = True   # NEVER change without explicit sign-off

# =============================================================================
# TELEGRAM
# =============================================================================

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
CHAT_ID: str   = os.getenv("CHAT_ID", "")

if not BOT_TOKEN or not CHAT_ID:
    logging.warning(
        "[SETTINGS] BOT_TOKEN or CHAT_ID missing. Telegram will fail. Check .env."
    )

# =============================================================================
# DIRECTORIES
# =============================================================================

DECISIONS_DIR  = BASE_DIR / "decisions"    # Per-day decision logs (JSON)
TRADES_DIR     = BASE_DIR / "trades"       # Per-day trade records (JSON)
LOGS_DIR       = BASE_DIR / "logs"         # Runtime logs
DATA_DIR       = BASE_DIR / "data"         # State files (trade_state.json etc.)

# Create all required dirs at import time
for _d in [DECISIONS_DIR, TRADES_DIR, LOGS_DIR, DATA_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

# State file — atomic JSON, survives process restarts
STATE_FILE     = DATA_DIR / "trade_state.json"
RISK_STATE_FILE      = DATA_DIR / "daily_risk_state.json"
DAILY_RISK_STATE_FILE = RISK_STATE_FILE   # Alias used by risk_engine.py

# =============================================================================
# STRATEGY — NIFTY POINTS (not percentages)
# =============================================================================

NIFTY_STRIKE_INTERVAL: int = 50      # ATM strike rounding interval

# ── Stop Loss ──────────────────────────────────────────────────────────────
STOP_LOSS_POINTS: int = 15            # Initial hard SL

# ── Targets ───────────────────────────────────────────────────────────────
# T1–T3 are fixed. T4+ follow T3 + N×25 formula.
TARGET_1_POINTS: int = 25             # T1: book 33%, move SL → breakeven
TARGET_2_POINTS: int = 40             # T2: book 33%, move SL → T1 level
TARGET_3_POINTS: int = 60             # T3: book 34%, SL → T2 level, hold rest

# Virtual target spacing after T3 (each level = previous + VIRTUAL_TARGET_STEP)
VIRTUAL_TARGET_STEP: int = 25         # T4=85, T5=110, T6=135, T7=160 ...
VIRTUAL_TARGET_MAX_LEVELS: int = 13   # T4 through T13 (10 virtual levels)

def get_target_points(n: int) -> int:
    """
    Return target level in Nifty points for target number n (1-based).
    T1=25, T2=40, T3=60, T4=85, T5=110, T6=135 ...
    """
    base_targets = {1: TARGET_1_POINTS, 2: TARGET_2_POINTS, 3: TARGET_3_POINTS}
    if n in base_targets:
        return base_targets[n]
    # T4+ = T3 + (n-3) * VIRTUAL_TARGET_STEP
    return TARGET_3_POINTS + (n - 3) * VIRTUAL_TARGET_STEP

# ── Trailing SL after each milestone ──────────────────────────────────────
# After T1 → SL moves to breakeven (entry)
# After T2 → SL moves to T1 level (entry + 25)
# After T3 → SL moves to T2 level (entry + 40)
# After Tn → SL moves to T(n-1) level
# This is computed dynamically in trade_manager.py using get_target_points()

# ── Reversal Detection ──────────────────────────────────────────────────────
# Trade is closed when BOTH conditions are true:
#   1. 5m candle direction flips against the open trade direction
#   2. Price is below EMA9 (for long) or above EMA9 (for short)
REVERSAL_REQUIRE_EMA_CONFIRM: bool = True  # Require EMA9 confirmation for reversal

# After T1 hit, SL moves to entry + this offset (not just breakeven)
# BULLISH: entry + 15,  BEARISH: entry - 15  → locks in +15pts minimum
SL_AFTER_T1_OFFSET: int = 15

# Partial booking fractions (informational analytics — no real execution)
# T1: show 1/3 booked, T2: show 1/3 booked, T3+: remainder runs with trailing SL
BOOKING_FRACTION: float = 1 / 3

# =============================================================================
# RISK ENGINE
# =============================================================================

ACCOUNT_CAPITAL: float       = 5_000.0   # Starting paper trading capital (INR)
NIFTY_LOT_SIZE: int          = 75        # NSE NIFTY options lot size
OPTION_DELTA: float          = 0.5       # Assumed ATM delta

MAX_RISK_PCT: float          = 20.0      # Max % of capital per trade
MAX_DAILY_LOSS_PCT: float    = 30.0      # Max % of capital as total daily loss
MAX_TRADES_PER_DAY: int      = 5         # Base daily trade cap
MAX_CONSECUTIVE_LOSSES: int  = 2         # Lock out after N consecutive losses
COOLDOWN_AFTER_SL_MINUTES: int = 30      # Wait after SL hit (same direction)
COOLDOWN_HIGH_CONF_OVERRIDE: bool = True  # HIGH confidence bypasses SL cooldown
COOLDOWN_REVERSAL_OVERRIDE: bool  = True  # Opposite-direction bypasses cooldown

# Trade extension via Telegram approval (when daily limit hit)
TRADE_EXTENSION_BATCH: int     = 2
TRADE_EXTENSION_MAX_TOTAL: int = 4
TRADE_EXTENSION_MIN_SCORE: int = 70

# =============================================================================
# CONFIDENCE SCORING
# =============================================================================

CONFIDENCE_VERY_HIGH_THRESHOLD: int = 85
CONFIDENCE_HIGH_THRESHOLD: int      = 70
CONFIDENCE_MED_THRESHOLD: int       = 45

SCORE_WEIGHT_TF_ALIGN: int  = 50
SCORE_WEIGHT_VWAP_DIST: int = 25
SCORE_WEIGHT_EMA_ALIGN: int = 25

SCALE_UP_MULTIPLIER: float = 2.0
SCALE_UP_MAX_LOTS: int     = 5

# =============================================================================
# TIMING (seconds)
# =============================================================================

SCAN_INTERVAL_SECONDS: int    = 300   # Signal loop — every 5 minutes
TRACKER_INTERVAL_SECONDS: int = 30    # Tracker loop — every 30 seconds (was 60)
HEARTBEAT_INTERVAL_SECONDS: int = 300 # Telegram heartbeat — every 5 minutes

# =============================================================================
# MARKET HOURS (IST, 24h)
# =============================================================================

MARKET_OPEN_HOUR: int    = 9
MARKET_OPEN_MINUTE: int  = 15
MARKET_CLOSE_HOUR: int   = 15
MARKET_CLOSE_MINUTE: int = 30

EOD_CLOSE_HOUR: int   = 15
EOD_CLOSE_MINUTE: int = 29    # Force-close all open trades at 15:29

# =============================================================================
# DATA ENGINE
# =============================================================================

NIFTY_TICKER: str           = "^NSEI"
DATA_CACHE_SECONDS: int     = 60
LIVE_PRICE_CACHE_SECONDS: int = 10
TIMEFRAMES: list            = ["5m", "15m", "1h"]
PRIMARY_TIMEFRAME: str      = "5m"
TF_WAIT_MS: int             = 2000
DATA_MIN_CANDLES: int       = 15

YFINANCE_TF_PARAMS: dict = {
    "5m":  ("7d",  "5m"),
    "15m": ("60d", "15m"),
    "1h":  ("60d", "1h"),
}

# =============================================================================
# NSE / OI API
# =============================================================================

NSE_BASE_URL: str         = "https://www.nseindia.com"
NSE_OPTION_CHAIN_URL: str = "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY"
NSE_INDEX_URL: str        = "https://www.nseindia.com/api/allIndices"

NSE_API_HEADERS: dict = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.nseindia.com/option-chain",
}

OI_PCR_BULLISH_THRESHOLD: float  = 1.3
OI_PCR_BEARISH_THRESHOLD: float  = 0.7
OI_ATM_RANGE_STRIKES: int        = 5
OI_SCORE_CONFIRM: int            = 8
OI_SCORE_CONTRADICT: int         = -15
OI_MAX_PAIN_GRAVITY_POINTS: float = 200.0
OI_MAX_PAIN_GRAVITY_PENALTY: int  = -5
OI_CACHE_SECONDS: int            = 300   # fresh cache window (5 min)
OI_STALE_CACHE_SECONDS: int      = 900   # stale fallback window (15 min)

# =============================================================================
# NEWS SENTIMENT
# =============================================================================

VIX_LOW_THRESHOLD: float      = 15.0
VIX_MODERATE_THRESHOLD: float = 20.0
VIX_HIGH_THRESHOLD: float     = 25.0
VIX_LOW_BONUS: int            = 5
VIX_MODERATE_PENALTY: int     = -10
VIX_HIGH_PENALTY: int         = -20

US_FUTURES_STRONG_UP_PCT: float   =  0.5
US_FUTURES_STRONG_DOWN_PCT: float = -0.5
US_FUTURES_CRASH_PCT: float       = -1.0
US_FUTURES_UP_BONUS: int          =  5
US_FUTURES_DOWN_PENALTY: int      = -10
US_FUTURES_CRASH_PENALTY: int     = -15
US_FUTURES_TICKER: str            = "ES=F"

SENTIMENT_CONFIRM_BONUS: int      =  3
SENTIMENT_CONTRADICT_PENALTY: int = -5
SENTIMENT_CACHE_SECONDS: int      = 300

RSS_FEEDS: dict = {
    "ET Markets":   "https://economictimes.indiatimes.com/markets/rss.cms",
    "Moneycontrol": "https://www.moneycontrol.com/rss/marketreports.xml",
    "CNBCTV18":     "https://www.cnbctv18.com/commonfeeds/v1/cne/rss/market.xml",
}

GOOGLE_NEWS_NIFTY_RSS: str = (
    "https://news.google.com/rss/search"
    "?q=NIFTY+stock+market&hl=en-IN&gl=IN&ceid=IN:en"
)

SENTIMENT_BULLISH_KEYWORDS: list = [
    "rally", "surge", "bullish", "gains", "rises", "climbs", "positive",
    "buying", "upside", "breakout", "record high", "recover", "strong",
]
SENTIMENT_BEARISH_KEYWORDS: list = [
    "fall", "drop", "bearish", "decline", "crash", "sell-off", "negative",
    "selling", "downside", "breakdown", "record low", "weak", "correction",
]

# =============================================================================
# NIFTY PRICE VALIDATION BOUNDS
# =============================================================================

NIFTY_PRICE_MIN: float               = 15_000.0
NIFTY_PRICE_MAX: float               = 35_000.0
NIFTY_INDICATOR_MAX_DEVIATION: float = 800.0

# =============================================================================
# TELEGRAM APPROVAL WINDOW
# =============================================================================

TELEGRAM_APPROVAL_TIMEOUT_MINUTES: int = 5
TELEGRAM_POLL_INTERVAL_SECONDS: int    = 3

# =============================================================================
# TECHNICAL INDICATORS — NEW MULTI-LAYER SIGNAL ENGINE
# =============================================================================

# ── RSI ────────────────────────────────────────────────────────────────────
RSI_PERIOD: int              = 14
RSI_BULL_IDEAL_MIN: float    = 52.0   # Ideal bull entry zone (momentum rising)
RSI_BULL_IDEAL_MAX: float    = 70.0   # Upper end of ideal bull zone
RSI_BEAR_IDEAL_MIN: float    = 30.0   # Lower end of ideal bear zone
RSI_BEAR_IDEAL_MAX: float    = 48.0   # Ideal bear entry zone (momentum falling)
RSI_OVERBOUGHT_BLOCK: float  = 75.0   # HARD BLOCK: RSI above this for bull → skip
RSI_OVERSOLD_BLOCK: float    = 25.0   # HARD BLOCK: RSI below this for bear → skip
RSI_WARN_UPPER: float        = 68.0   # Warning: approaching overbought (penalty)
RSI_WARN_LOWER: float        = 32.0   # Warning: approaching oversold (penalty)

# ── MACD ───────────────────────────────────────────────────────────────────
MACD_FAST: int   = 12
MACD_SLOW: int   = 26
MACD_SIGNAL: int = 9

# ── ADX ────────────────────────────────────────────────────────────────────
ADX_PERIOD: int              = 14
ADX_SIDEWAYS_BLOCK: float    = 18.0   # HARD BLOCK: ADX below this = sideways → no trade
ADX_TREND_THRESHOLD: float   = 22.0   # Emerging trend (penalty if below)
ADX_STRONG_THRESHOLD: float  = 30.0   # Strong trend (bonus)
ADX_VERY_STRONG: float       = 40.0   # Very strong (higher bonus)

# ── ATR — Dynamic Stop-Loss ────────────────────────────────────────────────
ATR_PERIOD: int              = 14
ATR_SL_MULTIPLIER: float     = 1.0    # SL = ATR * this multiplier
ATR_MIN_POINTS: float        = 10.0   # Minimum dynamic SL
ATR_MAX_POINTS: float        = 28.0   # Maximum dynamic SL (capped)

# ── EMA Stack (9 / 20 / 50) ──────────────────────────────────────────────
EMA_SHORT: int   = 9
EMA_MID: int     = 20
EMA_LONG: int    = 50

# ── Bollinger Bands ────────────────────────────────────────────────────────
BB_PERIOD: int   = 20
BB_STD: float    = 2.0

# ── Supertrend ─────────────────────────────────────────────────────────────
SUPERTREND_PERIOD: int        = 10
SUPERTREND_MULTIPLIER: float  = 3.0

# ── Opening Range Breakout (ORB) ──────────────────────────────────────────
ORB_MINUTES: int        = 15   # First 15 min of session forms the range
ORB_WINDOW_SKIP: bool   = True # Skip trades while ORB is still forming

# ── Market Structure ──────────────────────────────────────────────────────
MARKET_STRUCTURE_LOOKBACK: int = 12  # Candles to scan for HH/HL pattern

# =============================================================================
# NEW SCORING WEIGHTS  (replaces old SCORE_WEIGHT_* constants)
# Total base score: 0-100 points across 6 layers
# =============================================================================

SCORE_WEIGHT_TREND:     int = 25   # Layer 1: VWAP + Supertrend direction
SCORE_WEIGHT_TF_ALIGN:  int = 20   # Layer 2: 5m/15m/1h timeframe alignment
SCORE_WEIGHT_MOMENTUM:  int = 20   # Layer 3: RSI ideal zone + MACD histogram
SCORE_WEIGHT_EMA_STACK: int = 15   # Layer 4: EMA 9/20/50 stack aligned
SCORE_WEIGHT_MARKET_STRUCTURE: int = 10  # Layer 5: HH/HL (bull) or LH/LL (bear)
SCORE_WEIGHT_ORB:       int = 10   # Layer 6: Price beyond ORB in signal direction

# ── Per-layer bonus/penalty adjustments ────────────────────────────────────
SCORE_BONUS_ADX_STRONG:      int = 8    # ADX > 30
SCORE_BONUS_ADX_VERY_STRONG: int = 12   # ADX > 40
SCORE_BONUS_CANDLE_PATTERN:  int = 5    # Candlestick pattern confirms signal

SCORE_PENALTY_ADX_BORDERLINE: int = -8  # ADX 18-22 (weak but not blocking)
SCORE_PENALTY_RSI_WARN:        int = -8  # RSI approaching overbought/oversold
SCORE_PENALTY_LUNCH_HOUR:      int = -5  # During 12:00-13:00 (optional)

# ── Time-based filters ─────────────────────────────────────────────────────
LUNCH_AVOID_ENABLED: bool       = False  # Enable lunch-hour penalty
LUNCH_AVOID_START_HOUR: int     = 12
LUNCH_AVOID_START_MINUTE: int   = 0
LUNCH_AVOID_END_HOUR: int       = 13
LUNCH_AVOID_END_MINUTE: int     = 0


# =============================================================================
# GROWW BROKER API
# Credentials loaded from .env -- never hardcode in source.
# PAPER_TRADING_MODE must be True until live sign-off (see top of file).
# =============================================================================

GROWW_API_BASE_URL: str    = "https://api.groww.in/v1"
GROWW_API_KEY: str         = os.getenv("GROWW_API_KEY", "")
GROWW_ACCESS_TOKEN: str    = os.getenv("GROWW_ACCESS_TOKEN", "")
GROWW_API_TIMEOUT: int     = 10     # seconds per request
GROWW_ORDER_RETRY_MAX: int = 3      # retries on transient failures

# F&O order defaults
GROWW_EXCHANGE: str        = "NSE"
GROWW_PRODUCT: str         = "INTRADAY"   # INTRADAY or DELIVERY
GROWW_ORDER_TYPE: str      = "MARKET"     # MARKET or LIMIT
GROWW_VALIDITY: str        = "DAY"

# =============================================================================
# LOGGING
# =============================================================================

LOG_LEVEL: str    = os.getenv("LOG_LEVEL", "INFO")
LOG_FORMAT: str   = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
LOG_DATE_FMT: str = "%Y-%m-%d %H:%M:%S"


def configure_logging() -> None:
    """Configure root logger. Call once at startup in main.py."""
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FMT,
    )
