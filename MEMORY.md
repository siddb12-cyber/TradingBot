# TradingBot — Project Memory File
> Last updated: 2026-05-31 (Session 3)
> Owner: Sidhant (Sid) | Company: Haus and Kinder / Rivermoor / Samara Retail India
> Read this file at the start of every new session before touching any code.

---

## 1. PROJECT IDENTITY

| Item | Value |
|------|-------|
| Purpose | AI-assisted intraday NIFTY 50 options paper trading bot |
| Mode | PAPER TRADING ONLY (`PAPER_TRADING_MODE = True` — never change without sign-off) |
| Capital | ₹5,000 paper capital |
| Market | NSE NIFTY 50 options — ATM CE/PE buying only |
| Platform | Windows 11, Python 3.10+ |
| Broker (future) | Groww API — access being purchased June 2026 |

---

## 2. FOLDER STRUCTURE

```
TradingBot/
├── main.py                    ← Entry point; launches 3 threads + dashboard + tray
├── config/
│   └── settings.py            ← ALL config constants (single source of truth, 403 lines)
├── core/
│   ├── engine.py              ← 3-thread orchestrator + /ping /status /pnl handlers (420 lines)
│   ├── data_engine.py         ← Market data: NSE API + yfinance (829 lines)
│   ├── signal_engine.py       ← 6-layer MTF confidence scoring (842 lines)
│   ├── indicators.py          ← All technical indicators + 8 candlestick patterns (673 lines)
│   ├── risk_engine.py         ← Daily risk manager (loss limits, cooldowns, sizing)
│   ├── trade_manager.py       ← Trade state machine + dynamic ATR SL + broker hooks (647 lines)
│   ├── market_hours.py        ← NSE market hours + 2025/2026 holiday calendar
│   ├── oi_analysis.py         ← OI/PCR/MaxPain from NSE API + 5-min cache + 15-min stale fallback (474 lines)
│   └── news_sentiment.py      ← VIX + US Futures + RSS/Google News sentiment
├── broker/                    ← NEW (Session 3)
│   ├── __init__.py            ← Package init (21 lines)
│   ├── groww_client.py        ← Groww API stub — all methods stubbed, HTTP fill-in pending (476 lines)
│   └── order_manager.py       ← Routes through PAPER_TRADING_MODE gate (287 lines)
├── telegram/
│   ├── bot.py                 ← All Telegram messages + /ping /status /pnl reply methods (533 lines)
│   └── heartbeat.py           ← 5-min alive heartbeat to Telegram
├── analytics/
│   └── logger.py              ← Appends decisions + trades to daily JSON + load_today_trades() (186 lines)
├── dashboard/
│   ├── server.py              ← Flask API server (localhost:5050)
│   └── index.html             ← Single-page live dashboard (dark theme)
├── app/
│   ├── tray.py                ← Windows system tray icon (color-coded state)
│   ├── icons.py               ← Pillow icon renderer
│   └── notifier.py            ← Windows toast notifications
├── runtime/
│   └── telegram_reminder.py   ← 8:30 AM daily Telegram morning briefing
├── data/
│   ├── trade_state.json       ← Live trade state (atomic writes)
│   └── daily_risk_state.json  ← Daily P&L / risk counters
├── decisions/                 ← Per-day signal decision logs (JSON arrays)
├── trades/                    ← Per-day closed trade records (JSON arrays)
├── logs/                      ← Runtime + dashboard + watchdog logs
├── .env                       ← BOT_TOKEN + CHAT_ID (not committed)
├── .env.example               ← Template
├── requirements.txt           ← All Python dependencies
├── start.bat                  ← One-click launcher (auto-restart loop)
├── stop.bat                   ← Clean shutdown
└── MEMORY.md                  ← THIS FILE — project state tracker
```

---

## 3. ARCHITECTURE — HOW IT WORKS

### 3 threads running in parallel:
1. **Signal thread** (every 5 min, 09:15–15:30 IST) → fetches MTF data → scores confidence → sends Telegram approval
2. **Tracker thread** (every 30s) → monitors open trade → checks SL/targets → trailing SL → EOD close
3. **Telegram poller thread** (every 3s) → listens for APPROVE/SCALE/REJECT buttons AND /ping /status /pnl text commands

### Data flow:
```
DataEngine.get_analysis()
  → yfinance OHLCV (5m, 15m, 1h)
  → compute all indicators per timeframe
  → detect ORB, MarketStructure, CandlePattern
  → return MTF dict
        ↓
SignalEngine.compute(MTF)
  → 4 hard filters (ADX, RSI OB/OS, ORB window)
  → 6-layer score (0–100)
  → OI adjustment (±15)  [cached 5 min fresh / 15 min stale fallback]
  → Sentiment adjustment (±20)
  → return signal dict
        ↓
TelegramBot.send_approval(signal)
  → User presses APPROVE / SCALE 2x / REJECT
        ↓
TradeManager.open_pending(signal)
  → Dynamic ATR SL set
  → order_manager.place_entry(signal)  [PAPER: log only | LIVE: Groww API]
  → State saved to trade_state.json
        ↓
TradeManager.update() [every 30s]
  → Check SL/T1/T2/T3... → Trailing SL ladder
  → On hit → order_manager.place_exit(trade) → close trade → log to trades/
```

---

## 4. SIGNAL ENGINE — 6-LAYER SCORING

### Hard Filters (trade blocked if triggered):
| Filter | Condition |
|--------|-----------|
| ADX sideways | ADX < 18 |
| RSI overbought | RSI > 75 + BULLISH signal |
| RSI oversold | RSI < 25 + BEARISH signal |
| ORB window | First 15 min of session |

### 6-Layer Base Score (max 100 pts):
| Layer | Max Pts | What it measures |
|-------|---------|-----------------|
| L1 Trend direction | 25 | VWAP position (12) + Supertrend direction (13) |
| L2 TF alignment | 20 | 3/3=20, 2/3=12, 1/3=5 across 5m/15m/1h |
| L3 Momentum | 20 | RSI in ideal zone (12) + MACD histogram (8) |
| L4 EMA stack | 15 | 9/20/50 EMA fully aligned = 15, partial = 8 |
| L5 Market structure | 10 | HH/HL (bull) or LH/LL (bear) |
| L6 ORB | 10 | Price beyond ORB in signal direction |

### Bonuses / Penalties:
- ADX > 30 → +8 pts | ADX > 40 → +12 pts | Candle pattern confirms → +5 pts
- ADX 18–22 → −8 pts | RSI approaching OB/OS → −8 pts | Lunch hour → −5 pts

### OI Adjustment (±15) and Sentiment Adjustment (±20) applied last.

### Confidence Tiers:
| Score | Tier | Action |
|-------|------|--------|
| < 45 | LOW | Skip — no trade |
| 45–69 | MEDIUM | Trade with 1 lot |
| 70–84 | HIGH | Trade with 1 lot |
| ≥ 85 | VERY HIGH | Trade — eligible for SCALE approval |

---

## 5. TRADE STATE MACHINE

```
IDLE → open_pending(signal) → PENDING
PENDING → handle_approval("APPROVE") → OPEN
PENDING → handle_approval("SCALE")   → OPEN (lots × 2)
PENDING → handle_approval("REJECT")  → IDLE
OPEN → SL hit / target / EOD close  → CLOSED → IDLE
```

### Trailing SL Ladder (NIFTY index points):
| Level | Points from entry | Action |
|-------|-------------------|--------|
| SL | −Dynamic (ATR-based, 10–28 pts) | Hard stop |
| T1 | +25 | Book ⅓, SL → entry+15 |
| T2 | +40 | Book ⅓, SL → T1 level |
| T3 | +60 | Book ⅓, SL → T2 level, hold rest |
| T4+ | +85, +110, +135... (+25 each) | Trail SL to previous level |

### Dynamic SL:
- `sl_pts = ATR * ATR_SL_MULTIPLIER (1.0)`, capped 10–28 pts
- Falls back to fixed 15 pts if ATR unavailable

---

## 6. INDICATORS LIBRARY (`core/indicators.py`)

| Function | Returns |
|----------|---------|
| `compute_rsi(df, period=14)` | pd.Series |
| `compute_macd(df, fast=12, slow=26, signal=9)` | (macd, signal, histogram) |
| `compute_adx(df, period=14)` | pd.Series |
| `compute_adx_full(df, period=14)` | (ADX, +DI, -DI) |
| `compute_atr(df, period=14)` | pd.Series |
| `compute_bollinger_bands(df, period=20, std=2.0)` | (upper, mid, lower) |
| `compute_supertrend(df, period=10, multiplier=3.0)` | (supertrend_line, direction_series) |
| `compute_ema(df, span)` | pd.Series |
| `compute_opening_range(df, minutes=15)` | dict {orb_high, orb_low, orb_range, valid} |
| `detect_market_structure(df, lookback=12)` | "BULLISH" / "BEARISH" / "SIDEWAYS" |
| `detect_candlestick_pattern(df)` | dict {pattern, bias, strength} |

Candlestick patterns: DOJI, BULLISH_ENGULFING, BEARISH_ENGULFING, HAMMER, SHOOTING_STAR,
BULL_MARUBOZU, BEAR_MARUBOZU, PIN_BAR_BULL, PIN_BAR_BEAR, INSIDE_BAR, NONE

---

## 7. RISK ENGINE

| Parameter | Value |
|-----------|-------|
| Capital | ₹5,000 |
| Lot size | 75 (NIFTY) |
| Max risk per trade | 20% of capital |
| Max daily loss | 30% of capital |
| Max trades/day | 5 |
| Max consecutive losses | 2 (then lock out) |
| Cooldown after SL | 30 min (same direction) |
| HIGH confidence override | Bypasses cooldown |

---

## 8. BROKER LAYER (`broker/`)

Built as stubs — paper mode works now, live mode enabled by adding credentials + setting `PAPER_TRADING_MODE = False`.

### `broker/groww_client.py` (476 lines)
Methods stubbed (TODO markers for HTTP calls when credentials arrive):
- `authenticate()` → get access token
- `place_order(symbol, strike, option_type, qty, transaction_type, order_type)` → order_id
- `cancel_order(order_id)`
- `get_order_status(order_id)`
- `get_positions()` / `get_holdings()`

### `broker/order_manager.py` (287 lines)
- `place_entry(signal, lots)` → if PAPER: returns fake order_id; if LIVE: calls groww_client
- `place_exit(trade, reason)` → if PAPER: logs only; if LIVE: calls groww_client (SQ-off)

### Trade manager hooks:
- `open_pending()` calls `place_entry()` after Telegram approval
- `_close_trade()` calls `place_exit()` on SL/target/EOD

### Settings required in `.env` (for future live mode):
```
GROWW_API_KEY=<your_key>
GROWW_ACCESS_TOKEN=<your_token>
```

---

## 9. TELEGRAM COMMANDS

All implemented and wired into `core/engine.py` `_tg_poller`:

| Command | Handler | What it sends |
|---------|---------|---------------|
| `/ping` | `_handle_cmd_ping()` | Live NIFTY price + bot uptime |
| `/status` | `_handle_cmd_status()` | Risk counters (P&L, trades, consecutive losses) + active trade if open |
| `/pnl` | `_handle_cmd_pnl()` | Today's closed trades (entry, exit, P&L per trade + daily total) |

Reply methods in `telegram/bot.py`: `send_ping_reply()`, `send_status_reply()`, `send_pnl_reply()`

---

## 10. OI ANALYSIS CACHING

Two-tier cache in `core/oi_analysis.py`:

| Tier | Duration | Behaviour |
|------|----------|-----------|
| Fresh cache | 300s (5 min) | Serve cached data; don't fetch |
| Stale fallback | 900s (15 min) | If fresh fetch fails, serve old data with `source="stale_cache"` |
| Expired | > 900s | Return `score_adjustment=0`, `source="none"` |

Settings: `OI_CACHE_SECONDS=300`, `OI_STALE_CACHE_SECONDS=900`

---

## 11. KEY SETTINGS (`config/settings.py`)

```python
PAPER_TRADING_MODE = True          # NEVER change without sign-off
NIFTY_STRIKE_INTERVAL = 50
TIMEFRAMES = ["5m", "15m", "1h"]
PRIMARY_TIMEFRAME = "5m"
ADX_SIDEWAYS_BLOCK = 18.0
ATR_SL_MULTIPLIER = 1.0
ATR_MIN_POINTS = 10.0 / ATR_MAX_POINTS = 28.0
CONFIDENCE_MED_THRESHOLD = 45
CONFIDENCE_HIGH_THRESHOLD = 70
CONFIDENCE_VERY_HIGH_THRESHOLD = 85
OI_CACHE_SECONDS = 300
OI_STALE_CACHE_SECONDS = 900
GROWW_API_BASE_URL = "https://api.groww.in/v1"
GROWW_EXCHANGE = "NSE"
GROWW_PRODUCT = "INTRADAY"
GROWW_ORDER_TYPE = "MARKET"
```

---

## 12. KNOWN ISSUES / OPEN ITEMS

| Issue | Severity | Status |
|-------|----------|--------|
| Groww API HTTP calls | Medium | All methods are stubs — fill in when API key arrives (June 2026) |
| Heartbeat get_live_price() | Low | Dead code path (suppressed exception) — non-critical |
| .pyc cache invalidation | Low | Sandbox `.pyc` files can't be deleted (permissions). Always run `touch` on modified `.py` files before testing, or tests may use stale bytecode |

---

## 13. SESSION LOG

### Session 1: 2026-05-31 — Signal System Overhaul
Rebuilt signal engine from scratch: 6-layer scoring, hard filters, ADX/RSI/MACD/Supertrend/EMA/ORB/MarketStructure/Candlestick. Created `core/indicators.py` (673 lines). Full rewrite of `signal_engine.py` (842 lines). Major update to `data_engine.py` (829 lines). Updated `trade_manager.py`, `bot.py`, `settings.py`, `logger.py`.

### Session 2–3: 2026-05-31 — Broker Layer + Telegram Commands + OI Fix

**Files created:**
| File | Lines | What |
|------|-------|------|
| `broker/__init__.py` | 21 | Package |
| `broker/groww_client.py` | 476 | Groww API stub (all methods, TODO HTTP calls) |
| `broker/order_manager.py` | 287 | Paper/live routing gate |

**Files updated:**
| File | Lines | Change |
|------|-------|--------|
| `core/trade_manager.py` | 647 | broker hooks in open_pending() + _close_trade() + broker_order_id field |
| `telegram/bot.py` | 533 | send_ping_reply / send_status_reply / send_pnl_reply |
| `core/engine.py` | 420 | /ping /status /pnl command routing in _tg_poller |
| `analytics/logger.py` | 186 | load_today_trades() method |
| `core/oi_analysis.py` | 474 | 15-min stale cache fallback |
| `config/settings.py` | 403 | GROWW_* constants + OI_STALE_CACHE_SECONDS |

**All 11 files syntax-clean. All imports verified. Full integration test passed (7/7 checks).**

---

## 14. NEXT SESSION — WHAT TO BUILD

### Priority 1: Wire Groww HTTP calls (June 2026 — after credentials arrive)
When Groww API key is purchased, fill in the `TODO` blocks in `broker/groww_client.py`:
1. `authenticate()` — POST to Groww auth endpoint, store access token
2. `place_order()` — POST order to Groww F&O endpoint
3. `cancel_order()` / `get_order_status()` — standard REST calls
4. Update `.env.example` with `GROWW_API_KEY` and `GROWW_ACCESS_TOKEN`
5. Test with `PAPER_TRADING_MODE = False` in a staging environment

### Priority 2: Dashboard enhancements
The Flask dashboard at `localhost:5050` is functional but minimal. Consider:
- Live trade P&L card (updates every 30s via `data/trade_state.json`)
- Today's signal decisions log table
- Daily P&L chart (from `trades/` JSON files)

### Priority 3: Backtesting harness
Build `backtest/run.py` that:
- Loads historical OHLCV data (yfinance)
- Runs SignalEngine on each candle
- Simulates trade outcomes with the trailing SL ladder
- Outputs P&L curve + win rate + avg R:R

---

## 15. HOW TO START THE BOT

```bat
# Double-click:
start.bat

# Verify alive:
# Check Telegram for "💚 TradingBot Started" message
# Then send /ping to bot — should reply with live NIFTY price

# Dashboard:
http://localhost:5050

# Stop:
stop.bat
```

**Prerequisites**: `.env` file with `BOT_TOKEN` and `CHAT_ID` set.

---

## 16. INSTRUCTIONS FOR FUTURE SESSIONS

At the start of every new session:
1. Read this file (`MEMORY.md`) completely
2. Run the integrity check:
   ```
   python3 -c "import ast, os; [ast.parse(open(f).read()) or print('OK',f) for f in ['core/indicators.py','core/signal_engine.py','core/data_engine.py','core/trade_manager.py','core/engine.py','telegram/bot.py','analytics/logger.py','broker/groww_client.py','broker/order_manager.py','config/settings.py']]"
   ```
3. Touch all `.py` files before running tests (sandbox .pyc cache issue):
   ```
   find . -name "*.py" -exec touch {} \;
   ```
4. Continue from **Section 14 — Next Session**

After every session:
- Update Section 13 (session log) with what was built/changed
- Update Section 14 (next session) with new priorities
- Update the "Last updated" date at the top
