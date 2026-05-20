"""
config/config.py
================
Single source of truth for all TradingBot configuration.

All modules must import constants from here.
No module should hardcode paths, credentials, or strategy values.

Secrets are loaded from the .env file via python-dotenv.
BASE_DIR is resolved dynamically — project is portable across machines.
"""

import os
import logging
from pathlib import Path
from dotenv import load_dotenv

# =========================
# BASE DIRECTORY
# Resolves to TradingBot/ regardless of where Python is called from.
# =========================

BASE_DIR = Path(__file__).parent.parent.resolve()

# =========================
# LOAD .env SECRETS
# Must be called before any os.getenv() call below.
# =========================

_env_path = BASE_DIR / ".env"

if not _env_path.exists():
    logging.warning(
        f"[CONFIG] .env file not found at {_env_path}. "
        "Copy .env.example to .env and fill in your credentials."
    )

load_dotenv(dotenv_path=_env_path)

# =========================
# TELEGRAM
# =========================

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
CHAT_ID: str   = os.getenv("CHAT_ID", "")

if not BOT_TOKEN or not CHAT_ID:
    logging.warning(
        "[CONFIG] BOT_TOKEN or CHAT_ID is missing. "
        "Telegram messages will fail. Check your .env file."
    )

# =========================
# CHROME / PLAYWRIGHT
# =========================

CHROME_DEBUG_URL: str = os.getenv(
    "CHROME_DEBUG_URL",
    "http://127.0.0.1:9222"
)

# =========================
# TESSERACT
# =========================

TESSERACT_CMD: str = os.getenv(
    "TESSERACT_CMD",
    r"C:\Program Files\Tesseract-OCR\tesseract.exe"
)

# =========================
# DIRECTORY PATHS
# All paths are pathlib.Path objects.
# Use str(path) when a string is required (e.g. cv2.imread).
# =========================

SCREENSHOT_DIR  = BASE_DIR / "screenshots"
TRADE_LOG_DIR   = BASE_DIR / "trade_logs"
TEMP_DIR        = BASE_DIR / "temp"
DATA_DIR        = BASE_DIR / "data"
STRATEGIES_DIR  = BASE_DIR / "strategies"

# Trade state machine persistence file.
# JSON file — survives process restarts, written atomically.
STATE_FILE = DATA_DIR / "trade_state.json"

# Daily risk engine state file.
# Resets automatically when the date changes.
DAILY_RISK_STATE_FILE = DATA_DIR / "daily_risk_state.json"

# Ensure required runtime directories exist at import time.
for _dir in [SCREENSHOT_DIR, TRADE_LOG_DIR, TEMP_DIR, DATA_DIR]:
    _dir.mkdir(parents=True, exist_ok=True)

# =========================
# STRATEGY CONSTANTS
# All values are in NIFTY INDEX POINTS (not percentages).
# =========================

NIFTY_STRIKE_INTERVAL: int  = 50    # Round to nearest 50 for ATM strike
STOP_LOSS_POINTS: int        = 10   # Exit if price moves 10 pts against trade
TARGET_1_POINTS: int         = 15   # First partial target
TARGET_2_POINTS: int         = 25   # Second target
TARGET_3_POINTS: int         = 40   # Full target

# =========================
# RISK ENGINE
# All monetary values in INR. Lot size and delta for NSE NIFTY options.
# Adjust NIFTY_LOT_SIZE if NSE changes contract specs.
# =========================

ACCOUNT_CAPITAL: float          = 5_000.0   # Starting paper trading capital (INR)
MAX_RISK_PCT: float             = 20.0      # Max % of capital at risk per trade
MAX_DAILY_LOSS_PCT: float       = 30.0      # Max % of capital as total daily loss
MAX_TRADES_PER_DAY: int         = 3         # Hard limit on signals per session
COOLDOWN_AFTER_SL_MINUTES: int  = 30        # Minutes to wait after an SL hit (same-direction only)
COOLDOWN_HIGH_CONF_OVERRIDE: bool = True    # HIGH confidence (>=70) bypasses SL cooldown entirely
COOLDOWN_REVERSAL_OVERRIDE: bool  = True    # Opposite-direction signal with MEDIUM+ bypasses cooldown
MAX_CONSECUTIVE_LOSSES: int     = 2         # Lockout after this many losses in a row
NIFTY_LOT_SIZE: int             = 75        # NSE NIFTY options contract lot size
OPTION_DELTA: float             = 0.5       # Assumed ATM delta for premium P&L estimate

# =========================
# TIMING CONSTANTS (seconds)
# =========================

SCAN_INTERVAL_SECONDS: int    = 300  # Signal generation loop — every 5 minutes
TRACKER_INTERVAL_SECONDS: int = 60   # Live tracker loop — every 1 minute

# =========================
# MARKET HOURS (IST, 24h format)
# Only generate signals within these bounds.
# System clock must be set to IST (UTC+5:30). No timezone lib required.
# =========================

MARKET_OPEN_HOUR: int    = 9
MARKET_OPEN_MINUTE: int  = 15
MARKET_CLOSE_HOUR: int   = 15
MARKET_CLOSE_MINUTE: int = 30

# EOD auto-close: tracker forces trade closure at this time (one minute before market close)
EOD_CLOSE_HOUR: int   = 15
EOD_CLOSE_MINUTE: int = 29

# =========================
# OCR CONFIGURATION
# Crop regions are (y1, y2, x1, x2) tuples in pixels.
# Calibrated for a 1920x1080 TradingView chart at 100% zoom.
# Adjust if your screen resolution or chart layout differs.
# =========================

# Region containing current NIFTY price (top price bar)
OCR_PRICE_REGION: tuple     = (0, 80, 0, 900)

# Region containing VWAP and EMA9 indicator values
OCR_INDICATOR_REGION: tuple = (0, 220, 0, 750)

# Tesseract page segmentation mode and engine mode
# PSM 6 = Assume a uniform block of text
# OEM 3 = Use both LSTM and legacy engine (best accuracy)
TESSERACT_CONFIG_TEXT: str    = "--psm 6 --oem 3"
TESSERACT_CONFIG_NUMERIC: str = "--psm 6 --oem 3 -c tessedit_char_whitelist=0123456789,."

# Image upscale factor before OCR (higher = slower but more accurate)
OCR_UPSCALE_FACTOR: float = 2.5

# =========================
# NIFTY VALUE VALIDATION BOUNDS
# Extracted values outside these ranges are rejected as OCR errors.
# =========================

# Acceptable NIFTY index price range
NIFTY_PRICE_MIN: float = 15_000.0
NIFTY_PRICE_MAX: float = 35_000.0

# Max allowed deviation between price and VWAP/EMA9 (NIFTY points)
# If VWAP or EMA9 deviates more than this from current price, it's an OCR error.
NIFTY_INDICATOR_MAX_DEVIATION: float = 800.0

# =========================
# MULTI-TIMEFRAME ANALYSIS
# Timeframes analyzed before each signal. Order matters — 5m is primary.
# TF_SELECTOR_MAP: CSS text used by Playwright to click TradingView toolbar buttons.
# TF_WAIT_MS: milliseconds to wait after clicking timeframe before screenshotting.
# =========================

# Timeframes analyzed in order. Values must match TF_SELECTOR_MAP keys.
TIMEFRAMES: list = ["5m", "15m", "1h"]

# Primary timeframe — signal direction is anchored here.
PRIMARY_TIMEFRAME: str = "5m"

# Milliseconds to wait after switching timeframe before taking screenshot.
# TradingView needs time to re-render candles and indicator values.
TF_WAIT_MS: int = 2000

# TradingView timeframe toolbar button text (exact visible label in the toolbar).
# Playwright clicks the button whose text matches this string.
# Adjust if your TradingView UI language or layout differs.
TF_SELECTOR_MAP: dict = {
    "5m":  "5",    # TradingView shows "5" in the toolbar
    "15m": "15",   # TradingView shows "15"
    "1h":  "60",   # TradingView shows "60" in minutes notation
}

# =========================
# CONFIDENCE SCORING
# Score range: 0–100. Thresholds control trade gate.
# =========================

# Minimum confidence to open a trade (trades below this are blocked entirely)
CONFIDENCE_VERY_HIGH_THRESHOLD: int = 85   # >= 85 → VERY HIGH → scale-up suggestion triggered
CONFIDENCE_HIGH_THRESHOLD: int      = 70   # >= 70 → HIGH → trade allowed
CONFIDENCE_MED_THRESHOLD: int       = 45   # 45–69 → MEDIUM → optional trade (allowed)
# < 45 → LOW → trade rejected

# Scale-up suggestion when confidence is VERY HIGH
# This is a Telegram suggestion only — no auto-execution ever occurs.
# Suggested lots = floor(standard_lots * SCALE_UP_MULTIPLIER), capped at SCALE_UP_MAX_LOTS.
SCALE_UP_MULTIPLIER: float = 2.0    # 2x standard lots suggested at VERY HIGH confidence
SCALE_UP_MAX_LOTS: int     = 5      # Hard cap on suggested lots (safety guardrail)

# Weight applied to each scoring component (must sum to 100)
# Timeframe alignment: how many TFs agree with the primary signal direction
SCORE_WEIGHT_TF_ALIGN: int    = 50   # 50 pts max — primary scoring driver
# VWAP distance: price proximity to VWAP (closer = stronger conviction)
SCORE_WEIGHT_VWAP_DIST: int   = 25   # 25 pts max
# EMA alignment: EMA9 slope consistency across timeframes
SCORE_WEIGHT_EMA_ALIGN: int   = 25   # 25 pts max

# =========================
# LOGGING
# =========================

LOG_LEVEL: str    = os.getenv("LOG_LEVEL", "INFO")
LOG_FORMAT: str   = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
LOG_DATE_FMT: str = "%Y-%m-%d %H:%M:%S"

def configure_logging() -> None:
    """
    Call this once at application startup (in main.py or each module entry point).
    Configures root logger with console output at the level set in LOG_LEVEL.
    """
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FMT,
    )

# =========================
# TELEGRAM APPROVAL WORKFLOW
# Controls the approval gate used before live Groww order placement.
# Approval is polled via Telegram Bot getUpdates API.
# =========================

# Minutes to wait for APPROVE/REJECT reply before auto-cancelling the order
TELEGRAM_APPROVAL_TIMEOUT_MINUTES: int = 2

# Seconds between each getUpdates poll while waiting for approval
TELEGRAM_POLL_INTERVAL_SECONDS: int = 3

# =========================
# PAPER TRADING VALIDATION PHASE
# Active until 2026-05-31. Set PAPER_TRADING_MODE=False ONLY after
# validation phase is complete and sign-off is given.
# This is a config constant — not an env variable — so it cannot be
# overridden at runtime without a code change.
# =========================

# Master kill-switch for real order placement
# True  = all Groww order placement is permanently blocked
# False = live trading enabled (requires explicit code change + review)
PAPER_TRADING_MODE: bool = True

# Validation phase end date (YYYY-MM-DD)
PAPER_TRADING_VALIDATION_END: str = "2026-05-31"

# Session ID prefix for paper trade IDs
PAPER_SESSION_PREFIX: str = "PAPER"

# Metrics persistence file — cumulative across validation phase
PAPER_METRICS_FILE = DATA_DIR / "paper_validation_metrics.json"

# Dashboard log — appended daily, human-readable
PAPER_DASHBOARD_LOG = BASE_DIR / "trade_logs" / "paper_trading_dashboard.log"

# Readiness thresholds for live deployment recommendation
# All must be met for READY status
READINESS_MIN_TRADES:          int   = 20     # Minimum trades completed
READINESS_MIN_SIGNAL_ACCURACY: float = 0.55   # >= 55% trades hit T1 or better
READINESS_MAX_SL_RATIO:        float = 0.40   # <= 40% trades hit SL
READINESS_MIN_AVG_CONFIDENCE:  float = 60.0   # Average confidence >= 60/100
READINESS_MAX_CONSEC_LOSSES:   int   = 3      # Max consecutive losses < 3

# =========================
# OI ANALYSIS ENGINE
# Option chain data pulled from NSE unofficial API, with scraping fallback.
# PCR and max pain are the two primary signals.
# =========================

# NSE unofficial option chain API (no key required — requires session cookie)
NSE_BASE_URL: str          = "https://www.nseindia.com"
NSE_OPTION_CHAIN_URL: str  = "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY"
NSE_INDEX_URL: str         = "https://www.nseindia.com/api/allIndices"

# Browser-like headers required for NSE API (blocks bots without these)
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

# PCR thresholds — Put-Call Ratio interpretation
OI_PCR_BULLISH_THRESHOLD: float = 1.3   # PCR > 1.3 → market leans bullish
OI_PCR_BEARISH_THRESHOLD: float = 0.7   # PCR < 0.7 → market leans bearish

# Strikes to sum either side of ATM for PCR calculation
OI_ATM_RANGE_STRIKES: int = 5

# Score adjustments for OI confirmation / contradiction
OI_SCORE_CONFIRM: int    =  8   # OI agrees with signal direction
OI_SCORE_CONTRADICT: int = -15  # OI disagrees with signal direction

# Max pain gravity: if current price is this far from max pain, apply penalty
OI_MAX_PAIN_GRAVITY_POINTS: float = 200.0
OI_MAX_PAIN_GRAVITY_PENALTY: int  = -5

# Cache OI results for this many seconds (avoids hammering NSE API)
OI_CACHE_SECONDS: int = 300

# =========================
# NEWS SENTIMENT ENGINE
# VIX, RSS feeds, US Futures, Google News.
# All adjustments applied to base confidence score before threshold check.
# =========================

# India VIX thresholds (NSE publishes live VIX)
VIX_LOW_THRESHOLD:      float = 15.0   # Calm market  — slight bonus
VIX_MODERATE_THRESHOLD: float = 20.0   # Elevated     — penalty
VIX_HIGH_THRESHOLD:     float = 25.0   # High risk    — large penalty

VIX_LOW_BONUS:        int =   5   # VIX < 15
VIX_MODERATE_PENALTY: int = -10   # VIX 20-25
VIX_HIGH_PENALTY:     int = -20   # VIX > 25

# US Futures change thresholds (percentage)
US_FUTURES_STRONG_UP_PCT:   float =  0.5   # Futures up > 0.5%
US_FUTURES_STRONG_DOWN_PCT: float = -0.5   # Futures down > 0.5%
US_FUTURES_CRASH_PCT:       float = -1.0   # Futures down > 1.0%

US_FUTURES_UP_BONUS:      int =   5   # Up > 0.5%
US_FUTURES_DOWN_PENALTY:  int = -10   # Down > 0.5%
US_FUTURES_CRASH_PENALTY: int = -15   # Down > 1.0%

# US Futures ticker (Yahoo Finance)
US_FUTURES_TICKER: str = "ES=F"   # S&P 500 E-mini futures

# News sentiment keyword scoring
SENTIMENT_CONFIRM_BONUS:      int =  3   # Headlines support signal direction
SENTIMENT_CONTRADICT_PENALTY: int = -5  # Headlines oppose signal direction

# Cache sentiment results (seconds)
SENTIMENT_CACHE_SECONDS: int = 300

# RSS feed URLs — Indian financial news
RSS_FEEDS: dict = {
    "ET Markets":   "https://economictimes.indiatimes.com/markets/rss.cms",
    "Moneycontrol": "https://www.moneycontrol.com/rss/marketreports.xml",
    "CNBCTV18":     "https://www.cnbctv18.com/commonfeeds/v1/cne/rss/market.xml",
}

# Google News RSS for NIFTY headlines
GOOGLE_NEWS_NIFTY_RSS: str = (
    "https://news.google.com/rss/search"
    "?q=NIFTY+stock+market&hl=en-IN&gl=IN&ceid=IN:en"
)

# Headline sentiment keyword lists
SENTIMENT_BULLISH_KEYWORDS: list = [
    "rally", "surge", "bullish", "gains", "rises", "climbs", "positive",
    "buying", "upside", "breakout", "record high", "recover", "strong",
]
SENTIMENT_BEARISH_KEYWORDS: list = [
    "fall", "drop", "bearish", "decline", "crash", "sell-off", "negative",
    "selling", "downside", "breakdown", "record low", "weak", "correction",
]

# =========================
# API-BASED DATA ENGINE
# Replaces TradingView + Playwright + OCR pipeline.
# Uses yfinance for OHLCV and NSE API for live price.
# =========================

# Yahoo Finance ticker symbol for NIFTY 50 Index
NIFTY_TICKER: str = "^NSEI"

# Cache duration for OHLCV data (seconds). Prevents hammering yfinance.
# Signal loop runs every 5 minutes, so 60s cache is safe.
DATA_CACHE_SECONDS: int = 60

# Cache duration for live price (shorter TTL — tracker needs fresh price)
LIVE_PRICE_CACHE_SECONDS: int = 10

# yfinance period and interval per timeframe.
# Format: {timeframe: (period, interval)}
# "7d" + "5m"  = ~7 days of 5-minute candles (Yahoo limit for intraday)
# "60d" + "15m" = 60 days of 15-minute candles
# "60d" + "1h"  = 60 days of hourly candles
YFINANCE_TF_PARAMS: dict = {
    "5m":  ("7d",  "5m"),
    "15m": ("60d", "15m"),
    "1h":  ("60d", "1h"),
}

# Minimum candles required for a timeframe to be considered valid
# (EMA9 needs at least 9 candles to be meaningful)
DATA_MIN_CANDLES: int = 15

# =========================
# RUNTIME MANAGER
# =========================
# Chrome executable path (Windows default — override via .env CHROME_EXE_PATH)
CHROME_EXE_PATH: str = os.getenv(
    "CHROME_EXE_PATH",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
)

# TradingView Chrome profile — port 9222 (existing setup)
TRADINGVIEW_PROFILE_DIR: str = os.getenv(
    "TRADINGVIEW_PROFILE_DIR",
    r"C:\Users\siddh\AppData\Local\Google\Chrome\User Data\Profile 7",
)
TRADINGVIEW_URL: str = os.getenv(
    "TRADINGVIEW_URL",
    "https://www.tradingview.com/chart/?symbol=NSE%3ANIFTY&interval=5",
)
TRADINGVIEW_CDP_PORT: int = int(os.getenv("TRADINGVIEW_CDP_PORT", "9222"))

# Groww Chrome profile — port 9333 (separate instance)
GROWW_PROFILE_DIR: str = os.getenv(
    "GROWW_PROFILE_DIR",
    r"C:\Users\siddh\AppData\Local\Google\Chrome\User Data\Profile 8",
)
GROWW_FNO_URL: str = os.getenv(
    "GROWW_FNO_URL",
    "https://groww.in/trade/f-and-o",
)
GROWW_CDP_PORT: int = int(os.getenv("GROWW_CDP_PORT", "9333"))

# Browser readiness polling
BROWSER_READY_TIMEOUT_S:       int   = 30    # Max seconds to wait for CDP endpoint
BROWSER_READY_POLL_INTERVAL_S: float = 2.0   # Seconds between CDP readiness polls

# Watchdog and supervisor
WATCHDOG_POLL_INTERVAL_S: int = 15            # Seconds between health checks
MAX_CRASH_RESTARTS:       int = 3             # Before halting a module permanently
CRASH_RESTART_BACKOFF_S:  int = 30            # Base backoff (doubles each restart)
MAX_BACKOFF_S:            int = 300           # Cap backoff at 5 minutes

# Heartbeat
HEARTBEAT_DIR: Path = BASE_DIR / "runtime" / "heartbeats"
HEARTBEAT_STALE_S: int = 120                  # Seconds before heartbeat considered stale
HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)

# Session persistence
RUNTIME_SESSION_FILE: Path = DATA_DIR / "runtime_session.json"
RUNTIME_LOG_FILE:     Path = BASE_DIR / "trade_logs" / "runtime_manager.log"

# Supervised module registry
# Each entry: (name, python_module_string, restart_on_crash)
SUPERVISED_MODULES: list = [
    {"name": "ai_trading_assistant",  "module": "core.ai_trading_assistant",  "restart": True},
    {"name": "live_trade_tracker",    "module": "core.live_trade_tracker",     "restart": True},
]

# Windows Task Scheduler task name for startup integration
STARTUP_TASK_NAME: str = "TradingBot_RuntimeManager"

# =========================
# OI ANALYSIS ENGINE
# =========================

NSE_BASE_URL          = "https://www.nseindia.com"
NSE_OPTION_CHAIN_URL  = "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY"
NSE_INDEX_URL         = "https://www.nseindia.com/api/allIndices"

NSE_API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.nseindia.com/option-chain",
}

OI_PCR_BULLISH_THRESHOLD  = 1.3
OI_PCR_BEARISH_THRESHOLD  = 0.7
OI_ATM_RANGE_STRIKES      = 5
OI_SCORE_CONFIRM          = 8
OI_SCORE_CONTRADICT       = -15
OI_MAX_PAIN_GRAVITY_POINTS = 200.0
OI_MAX_PAIN_GRAVITY_PENALTY = -5
OI_CACHE_SECONDS          = 300

# =========================
# NEWS SENTIMENT ENGINE
# =========================

VIX_LOW_THRESHOLD       = 15.0
VIX_MODERATE_THRESHOLD  = 20.0
VIX_HIGH_THRESHOLD      = 25.0
VIX_LOW_BONUS           = 5
VIX_MODERATE_PENALTY    = -10
VIX_HIGH_PENALTY        = -20

US_FUTURES_STRONG_UP_PCT   = 0.5
US_FUTURES_STRONG_DOWN_PCT = -0.5
US_FUTURES_CRASH_PCT       = -1.0
US_FUTURES_UP_BONUS        = 5
US_FUTURES_DOWN_PENALTY    = -10
US_FUTURES_CRASH_PENALTY   = -15
US_FUTURES_TICKER          = "ES=F"

SENTIMENT_CONFIRM_BONUS      = 3
SENTIMENT_CONTRADICT_PENALTY = -5
SENTIMENT_CACHE_SECONDS      = 300

RSS_FEEDS = {
    "ET Markets":   "https://economictimes.indiatimes.com/markets/rss.cms",
    "Moneycontrol": "https://www.moneycontrol.com/rss/marketreports.xml",
    "CNBCTV18":     "https://www.cnbctv18.com/commonfeeds/v1/cne/rss/market.xml",
}

GOOGLE_NEWS_NIFTY_RSS = (
    "https://news.google.com/rss/search"
    "?q=NIFTY+stock+market&hl=en-IN&gl=IN&ceid=IN:en"
)

SENTIMENT_BULLISH_KEYWORDS = [
    "rally", "surge", "bullish", "gains", "rises", "climbs", "positive",
    "buying", "upside", "breakout", "record high", "recover", "strong",
]
SENTIMENT_BEARISH_KEYWORDS = [
    "fall", "drop", "bearish", "decline", "crash", "sell-off", "negative",
    "selling", "downside", "breakdown", "record low", "weak", "correction",
]
