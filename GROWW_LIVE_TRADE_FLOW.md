# Groww Live Trade Flow — End-to-End Documentation

**TradingBot · Samara Retail India · Haus & Kinder**
Last updated: 21 May 2026

---

## Overview

This document covers the exact path a trade takes from signal generation to a real order placed on your Groww F&O account. Every step, every safety gate, and every config change required to go live is documented here.

**Current status: PAPER TRADING MODE (safe)**
`PAPER_TRADING_MODE = True` in `config/config.py` — no real orders are placed under any circumstances until you explicitly change this.

---

## The Full Pipeline

```
yfinance + NSE API
        ↓
  DataEngine.get_analysis()           [every 5 min]
        ↓
  SignalEngine.compute()              [score 0–100]
        ↓
  8-Gate Decision Pipeline            [trading_engine.py]
        ↓
  TelegramApprovalBot.send_signal()   [BLOCKING — waits for your tap]
        ↓
  YOU TAP ✅ APPROVE on Telegram
        ↓
  GrowwExecutor.execute_order()       [Playwright → Chrome CDP]
        ↓
  Groww F&O Order Placed              [BUY CE or PE, MARKET order]
        ↓
  Telegram confirmation message       ["✅ Groww Order PLACED"]
        ↓
  LiveTracker monitors price          [every 60 sec]
        ↓
  SL / Target / EOD auto-close        [Telegram notification]
        ↓
  DecisionLogger records everything   [Excel + Dashboard]
```

---

## Phase 1 — Signal Generation (Automatic)

Every 5 minutes, between 09:15 and 15:30 IST, the bot runs this pipeline:

### Gate 1 — Market Hours
```python
if not (09:15 <= now <= 15:30):
    skip  # No signals outside market hours
```

### Gate 2 — Engine State
```python
if state != IDLE:
    skip  # Already in a trade or waiting for approval
```

### Gate 3 — DataEngine Analysis
```python
analysis = data_engine.get_analysis()
# Returns: valid, is_trade, direction, alignment_count, alignment_summary, timeframe_data
```

Data sources:
- **Live price**: NSE API (`nseindia.com/api/allIndices`) → yfinance fallback
- **OHLCV**: yfinance (`^NSEI`, 5m/15m/1h intervals, cached 60s)
- **VWAP**: `(H+L+C)/3 × Volume / cumVol` reset daily
- **EMA9**: `Close.ewm(span=9, adjust=False).mean()`

### Gate 4 — Data Validity
```python
if not analysis['valid']:
    skip  # Insufficient candles or stale data
```

### Gate 5 — Direction + is_trade
```python
if not analysis['is_trade']:
    skip  # Sideways market, no clear bias
```
Sideways = fewer than 2 timeframes agree on direction.

### Gate 6 — Confidence Threshold
```python
if confidence_level == "LOW":   # score < 45
    skip  # Blocked entirely — not sent to Telegram
```

Confidence bands:
| Score | Level | Trade Allowed |
|-------|-------|---------------|
| < 45  | LOW   | No — blocked  |
| 45–69 | MEDIUM | Yes — normal |
| 70–84 | HIGH  | Yes           |
| ≥ 85  | VERY HIGH | Yes + Scale-up suggestion |

### Gate 7 — Risk Engine
```python
risk_engine.check_trade_allowed()
# Blocks if: daily loss exceeded, max trades reached, cooldown active,
#            consecutive losses >= MAX_CONSECUTIVE_LOSSES
```

Risk limits (from config.py):
- Max 3 trades per day
- Max 30% daily loss
- 30-min cooldown after SL hit (HIGH/VERY HIGH confidence bypasses this)
- Lockout after 2 consecutive losses

### Gate 8 — Telegram Approval (BLOCKING)
Signal is only sent to you after passing all 7 gates above.

---

## Phase 2 — Telegram Approval (You Decide)

When a valid signal passes all gates, you receive a Telegram message like this:

```
━━━━━━━━━━━━━━━━━━━━━━
📊 TRADE SIGNAL  ·  PAPER MODE
━━━━━━━━━━━━━━━━━━━━━━
Signal:     BUY 23800 CE
Direction:  BULLISH / CE BIAS
Price:      ₹23,792.50
VWAP:       ₹23,740.10  (+52.4 pts above)
EMA9:       ₹23,775.30  (+17.2 pts above)

📈 TF Alignment: 3/3 BULLISH
  5m  → BULLISH
  15m → BULLISH
  1h  → BULLISH

Score:      82/100  [HIGH]
OI Adj:     +8
Sent Adj:   +3
Final:      ✅ TRADE RECOMMENDED

Lots:  1  (75 qty)
SL:    10 pts  |  T1: 15  T2: 25  T3: 40
━━━━━━━━━━━━━━━━━━━━━━
ID: PAPER-20260521-003
Expires in 2 minutes
```

**Inline keyboard buttons:**
- `✅ APPROVE` — places order immediately (paper: logs it; live: Groww execution)
- `❌ REJECT` — discards signal, resets to IDLE

**At VERY HIGH confidence (≥85), an extra button appears:**
- `📈 SCALE x2` — approves with 2× lots (e.g., 2 lots instead of 1)

**If no response within 2 minutes:** signal auto-expires, state resets to IDLE.

**Trade management messages (while trade is open):**
- `🔒 Tighten SL` — moves SL closer to current price
- `📈 Trail SL` — activates trailing stop
- `❌ Close Now` — immediate forced exit

---

## Phase 3 — Groww Execution (Live Mode Only)

### How Groww Execution Works

When `PAPER_TRADING_MODE = False` and you tap APPROVE, `GrowwExecutor.execute_order()` is called. It uses **Playwright CDP** (Chrome DevTools Protocol) to control a Chrome window where Groww is already open and logged in.

**What Playwright does, step by step:**

```
1. Connect CDP → http://127.0.0.1:9333
2. Find Groww tab (url contains "groww.in")
   └─ If not found → open https://groww.in/trade/f-and-o in new tab
3. Click search box
4. Type "NIFTY 23800 CE" (parsed from signal)
5. Wait 1.5s for autocomplete → click first result
6. Find quantity input → type "1" (or "2" if SCALE)
7. Set order type → MARKET
8. Click BUY button
9. Click Confirm / Place Order button
10. Wait 1.5s for confirmation toast
11. Take screenshot → screenshots/groww_order_YYYYMMDD_HHMMSS.png
12. Send Telegram: "✅ Groww Order PLACED"
```

### Chrome Setup for Groww (Required for Live Mode)

Groww must be running in a **separate Chrome profile** with remote debugging enabled on port 9333.

**Create a shortcut with these parameters:**
```
"C:\Program Files\Google\Chrome\Application\chrome.exe"
  --remote-debugging-port=9333
  --user-data-dir="C:\Users\siddh\AppData\Local\Google\Chrome\User Data\Profile 8"
  https://groww.in/trade/f-and-o
```

You only need to do this once. After setting it up:
1. Double-click the shortcut
2. Log in to Groww F&O if not already logged in
3. Leave the Chrome window open in the background
4. The bot connects to it via CDP when a trade is approved

**To verify Chrome is ready:**
```powershell
# Run in PowerShell — should return JSON with tab list
Invoke-WebRequest http://127.0.0.1:9333/json | Select-Object Content
```

---

## Phase 4 — Live Tracking

After order placement (paper or live), the tracker thread runs every 60 seconds:

```python
current_price = data_engine.get_live_price()
# Checks against: SL, Target 1, Target 2, Target 3, EOD (15:29)
```

On any exit condition:
1. Records exit price, time, outcome in `trade_logs/trade_log_YYYY-MM-DD.xlsx`
2. Sends Telegram notification
3. Resets engine state to IDLE

---

## How to Switch to Live Trading

### Step 1 — Meet the readiness checklist

All of these must be true before going live:

| Readiness Metric | Required | Your Current |
|------------------|----------|--------------|
| Minimum trades completed | ≥ 20 | ~7 |
| Signal accuracy (T1+ hit rate) | ≥ 55% | Track in dashboard |
| SL hit ratio | ≤ 40% | Track in dashboard |
| Average confidence score | ≥ 60/100 | 56 (close) |
| Max consecutive losses | < 3 | Monitor |

### Step 2 — Set up Chrome for Groww (one time)

Create the Chrome shortcut described above. Log in to Groww F&O. Verify CDP is accessible at port 9333.

### Step 3 — Change exactly one line in config.py

```python
# config/config.py — line 273
# Change this:
PAPER_TRADING_MODE: bool = True

# To this:
PAPER_TRADING_MODE: bool = False
```

**This is the only change needed.** Every other system is already wired for live trading.

### Step 4 — Restart the bot

```
Double-click start_hidden.vbs
```

You will see in the startup Telegram message:
```
⚠️  LIVE TRADING MODE — orders will be placed on Groww
```

### Step 5 — First live trade

- Run at least 1 day in live mode with **1 lot** before scaling up
- Watch the Groww order book after each APPROVE
- If anything looks wrong, tap `❌ Close Now` on Telegram immediately
- Screenshot evidence is saved automatically to `screenshots/`

---

## Safety Gates Summary

| Gate | Location | What it blocks |
|------|----------|----------------|
| 1. Paper mode flag | `config.py` line 273 | All real execution |
| 2. execute_order() guard | `groww_executor.py` line 142 | Raises RuntimeError if paper mode |
| 3. trading_engine.py check | `trading_engine.py` | Only calls GrowwExecutor if not paper mode |
| 4. Telegram APPROVE required | `telegram_approval_bot.py` | Bot never auto-approves — you must tap |
| 5. 2-minute expiry | `telegram_approval_bot.py` | Signal dies if you don't respond |
| 6. Risk engine | `risk_engine.py` | Daily limits, consecutive loss lockout |
| 7. Confidence threshold | `signal_engine.py` | LOW confidence never reaches Telegram |

**No trade can be placed without your explicit tap on Telegram. This is non-negotiable.**

---

## Rollback — How to Return to Paper Mode

If anything goes wrong in live mode:

```python
# config/config.py — change back:
PAPER_TRADING_MODE: bool = True
```

Restart the bot. All execution stops immediately on the next signal cycle.

---

## Files Involved

| File | Role |
|------|------|
| `config/config.py` | Master config — PAPER_TRADING_MODE lives here |
| `core/trading_engine.py` | 8-gate decision pipeline + 3-thread orchestrator |
| `core/signal_engine.py` | Scoring, confidence calculation |
| `core/data_engine.py` | yfinance + NSE API data fetching |
| `core/telegram_approval_bot.py` | Inline keyboard approval flow |
| `core/groww_executor.py` | Playwright CDP order placement |
| `core/risk_engine.py` | Daily limits, consecutive loss tracking |
| `analytics/decision_logger.py` | 25-column Excel decision log per day |
| `analytics/generate_dashboard.py` | Run after each session to refresh dashboard |
| `trade_logs/` | All trade and decision Excel files |
| `docs/index.html` | Live dashboard (GitHub Pages) |
| `dashboard.html` | Local dashboard (open in browser) |
| `screenshots/` | Order evidence screenshots |
