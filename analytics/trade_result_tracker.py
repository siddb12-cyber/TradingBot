import pandas as pd
import os

# =========================
# LOAD TODAY LOG FILE
# =========================

today = "2026-05-15"

log_file = rf"C:\Users\siddh\Downloads\HK\TradingBot\trade_logs\trade_log_{today}.xlsx"

if not os.path.exists(log_file):

    print("Trade log file not found.")

    exit()

# =========================
# READ LOG
# =========================

df = pd.read_excel(log_file)

print(df)

# =========================
# CHECK LAST TRADE
# =========================

last_trade = df.iloc[-1]

trend = last_trade["Trend"]

entry_price = float(last_trade["Current Price"])

trade_signal = last_trade["Trade Signal"]

print("\n===== LAST TRADE =====")

print(f"Trend: {trend}")

print(f"Entry Price: {entry_price}")

print(f"Signal: {trade_signal}")

# =========================
# MOCK CURRENT PRICE
# =========================

# Replace later with live extraction

current_market_price = 23640

print(f"\nCurrent Market Price: {current_market_price}")

# =========================
# CALCULATE MOVE
# =========================

if "PE" in trade_signal:

    points_move = entry_price - current_market_price

elif "CE" in trade_signal:

    points_move = current_market_price - entry_price

else:

    points_move = 0

print(f"Points Move: {points_move}")

# =========================
# RESULT LOGIC
# =========================

result = "NO TRADE"

if points_move >= 40:

    result = "TARGET 3 HIT"

elif points_move >= 25:

    result = "TARGET 2 HIT"

elif points_move >= 15:

    result = "TARGET 1 HIT"

elif points_move <= -10:

    result = "STOP LOSS HIT"

else:

    result = "TRADE ACTIVE"

print(f"\nTRADE RESULT: {result}")