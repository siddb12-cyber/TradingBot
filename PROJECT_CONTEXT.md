# AI Intraday Trading Assistant — Master Context for Claude Co-Work

## Project Goal

Build a modular AI-assisted intraday paper trading system for NIFTY options trading using:

- TradingView
- Python
- Playwright
- OCR (Tesseract)
- Telegram Bot
- Claude Co-Work
- Future Groww integration

The system should:

- Monitor TradingView charts automatically
- Read VWAP + EMA9 + price structure
- Detect bullish/bearish setups
- Suggest CE/PE trades
- Track live trades
- Send Telegram updates
- Log all trades
- Generate analytics
- Eventually support:
  - OI analysis
  - Fibonacci
  - News sentiment
  - Multi-timeframe analysis
  - BankNifty
  - Position sizing
  - Risk engine
  - Real execution layer

IMPORTANT:
Currently ONLY PAPER TRADING.
NO REAL EXECUTION.

---

# USER ENVIRONMENT

## Operating System
Windows 11

## Project Root

```text
C:\Users\siddh\Downloads\HK\TradingBot
```

---

# CURRENT FOLDER STRUCTURE

```text
TradingBot/
│
├── main.py
│
├── core/
│   ├── __init__.py
│   ├── ai_trading_assistant.py
│   ├── live_trade_tracker.py
│   ├── market_structure_detector.py
│
├── extraction/
│   ├── value_extractor.py
│   ├── real_price_extractor.py
│   ├── trend_decision_engine.py
│
├── automation/
│   ├── open_tradingview.py
│   ├── auto_screenshot_engine.py
│   ├── screenshot_test.py
│
├── analytics/
│   ├── trade_result_tracker.py
│   ├── daily_performance_report.py
│
├── config/
│   ├── __init__.py
│   ├── config.py
│
├── screenshots/
├── trade_logs/
├── strategies/
├── data/
├── temp/
│
└── __pycache__/
```

---

# CURRENT WORKING FEATURES

## 1. TradingView Automation

Using:
- Playwright
- Chrome remote debugging
- Existing TradingView profile

The system:
- Opens TradingView
- Uses bookmarked NIFTY chart
- Uses:
  - 5-minute timeframe
  - VWAP
  - EMA9

---

# CHROME PROFILE

## Chrome Debug Command

```bash
chrome.exe --remote-debugging-port=9222 --user-data-dir="C:\Users\siddh\AppData\Local\Google\Chrome\User Data\Profile 7"
```

## Profile Path

```text
C:\Users\siddh\AppData\Local\Google\Chrome\User Data\Profile 7
```

---

# OCR ENGINE

Uses:

- pytesseract
- cv2
- screenshot cropping

Extracts:
- current price
- VWAP
- EMA9

---

# CURRENT STRATEGY LOGIC

## Bullish Logic

If:

```python
current_price > vwap and current_price > ema
```

Then:

```text
BULLISH / CE BIAS
BUY ATM CE
```

---

## Bearish Logic

If:

```python
current_price < vwap and current_price < ema
```

Then:

```text
BEARISH / PE BIAS
BUY ATM PE
```

---

## Sideways Logic

Else:

```text
NO TRADE
```

---

# CURRENT TARGET SYSTEM

IMPORTANT:
Targets are NOT percentages.

They are:

## NIFTY POINTS

Example:

```text
Target 1 = 15 Nifty points
Target 2 = 25 Nifty points
Target 3 = 40 Nifty points
```

This is intentional.

Option premiums move much faster than NIFTY.

---

# CURRENT STOP LOSS

```text
10 NIFTY points
```

---

# TELEGRAM BOT

Bot Name:

```text
Sid Trading Assistant
```

Username:

```text
siddb12_trading_bot
```

Telegram integration already working.

---

# CURRENT SYSTEM FLOW

```text
TradingView
    ↓
Screenshot Engine
    ↓
OCR Extraction
    ↓
Trend Detection
    ↓
Trade Signal
    ↓
Telegram Alert
    ↓
Trade Logging
    ↓
Live Tracking
    ↓
Analytics
```

---

# CURRENT FEATURES IMPLEMENTED

## Working

- TradingView screenshots
- OCR extraction
- VWAP reading
- EMA9 reading
- Trend detection
- CE/PE suggestions
- Strike calculation
- Telegram alerts
- Trade logging
- Live trade tracker
- Daily analytics
- Multi-process architecture
- main.py orchestration

---

# CURRENT FILE RESPONSIBILITIES

## core/ai_trading_assistant.py

Responsible for:
- screenshots
- OCR
- trend detection
- trade generation
- Telegram signals
- trade logging

---

## core/live_trade_tracker.py

Responsible for:
- monitoring latest trade
- tracking live price
- target hit detection
- SL detection
- Telegram trade updates

---

## analytics/daily_performance_report.py

Responsible for:
- daily analytics
- win rate
- trade summaries
- points captured

---

## main.py

Responsible for:
- launching system
- orchestrating modules
- managing processes

---

# CURRENT LIMITATIONS

The system currently DOES NOT:

- perform real execution
- use broker APIs
- calculate quantity sizing
- perform OI analysis
- use Fibonacci
- use news sentiment
- use multi-timeframe confirmation
- use risk management engine
- use confidence scoring
- manage multiple simultaneous trades

---

# HIGH PRIORITY FUTURE FEATURES

## 1. Position Sizing Engine

Need:

Given:

```text
Capital = ₹5000
Risk per trade = 2%
```

Calculate:

- max loss
- quantity
- lot sizing

---

## 2. Risk Engine

Need:

- max trades/day
- daily SL
- cooldowns
- avoid revenge trading
- avoid sideways markets

---

## 3. Multi-Timeframe Analysis

Need:

- 5m
- 15m
- 1h

confirmation system.

---

## 4. News Sentiment Engine

Need:

Integrations:

- Moneycontrol
- CNBC
- Economic Times
- US Futures
- Dollar Index
- VIX
- RBI/Fed news

Goal:

Detect:

- bullish sentiment
- bearish sentiment
- high-risk event days

---

## 5. OI Analysis Engine

Need:

- PCR
- OI buildup
- unwinding
- max pain
- CE/PE writing

---

## 6. Fibonacci Engine

Need:

- retracement levels
- support/resistance
- pullback entries

---

## 7. Groww Integration

Future goal:

After paper trading validation:

- broker integration
- semi-automatic execution
- approval-based execution

NO AUTO LIVE EXECUTION WITHOUT MANUAL APPROVAL.

---

# CURRENT DEVELOPMENT STYLE

IMPORTANT:

Claude should:

- generate complete files
- avoid partial snippets
- avoid requiring manual edits repeatedly
- generate production-style modular code
- maintain folder structure
- use scalable architecture
- avoid random standalone scripts

---

# CODING RULES

## Use modular architecture

- imports should work properly
- use package-based structure
- avoid hardcoded relative issues

---

## Use clear logging

Every major action should print:

```python
print("Connected to TradingView")
print("Trade Logged")
print("Telegram Sent")
```

---

## Use comments heavily

Every major section should contain:

```python
# =========================
# SECTION NAME
# =========================
```

---

## Use full scripts

Never provide only partial code unless specifically requested.

---

# CURRENT USER OBJECTIVE

User wants:

- AI-assisted intraday paper trading system
- eventually semi-automated execution
- learning-oriented setup
- professional architecture
- Telegram-based workflow
- low-capital starting strategy

Starting capital planned:

```text
₹5000
```

Current mode:

```text
Paper Trading Only
```

---

# IMPORTANT SYSTEM PHILOSOPHY

This project is NOT:

```text
One giant AI model
```

It is:

```text
Market Data Layer
↓
Extraction Layer
↓
Signal Layer
↓
Decision Layer
↓
Tracking Layer
↓
Analytics Layer
↓
Future AI Reasoning Layer
```

Claude should maintain this architecture.

---

# WHAT CLAUDE SHOULD DO NEXT

Claude should now help:

1. Improve architecture
2. Build stable modular code
3. Add advanced technical analysis
4. Add OI analysis
5. Add news engine
6. Add risk engine
7. Add quantity sizing
8. Add confidence scoring
9. Add BankNifty support
10. Improve OCR accuracy
11. Improve Telegram UX
12. Add dashboard/UI later
13. Add database storage later
14. Improve analytics
15. Build execution approval system

---

# VERY IMPORTANT SAFETY RULE

The system must:

- NEVER blindly auto-execute real trades
- ALWAYS support manual confirmation
- ALWAYS maintain risk controls
- ALWAYS prioritize paper trading validation first

---

# END OF MASTER CONTEXT

