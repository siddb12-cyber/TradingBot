"""
test_telegram.py
================
Run this once to verify your Telegram bot credentials work correctly.
Usage: python test_telegram.py
"""

import requests
import sys

BOT_TOKEN = "8861580303:AAHf_Y2yljKN9EEAkaz9_pcpZE6Aeanb6e4"
CHAT_ID   = "8331670846"

print("\n" + "="*55)
print("  TradingBot — Telegram Connection Test")
print("="*55)

# ── Step 1: Verify bot token is valid ─────────────────────
print("\n[1/3] Checking bot token...")
try:
    r = requests.get(
        f"https://api.telegram.org/bot{BOT_TOKEN}/getMe",
        timeout=10
    )
    data = r.json()
    if data.get("ok"):
        bot = data["result"]
        print(f"  [OK] Bot is valid: @{bot['username']} ({bot['first_name']})")
    else:
        print(f"  [ERROR] Invalid bot token: {data.get('description')}")
        print("  Fix: Create a new bot via @BotFather on Telegram.")
        sys.exit(1)
except Exception as e:
    print(f"  [ERROR] Could not reach Telegram API: {e}")
    print("  Fix: Check internet connection.")
    sys.exit(1)

# ── Step 2: Verify chat ID ────────────────────────────────
print("\n[2/3] Checking chat ID...")
try:
    r = requests.get(
        f"https://api.telegram.org/bot{BOT_TOKEN}/getChat",
        params={"chat_id": CHAT_ID},
        timeout=10
    )
    data = r.json()
    if data.get("ok"):
        chat = data["result"]
        print(f"  [OK] Chat found: {chat.get('first_name', '')} {chat.get('last_name', '')} (ID: {chat['id']})")
    else:
        print(f"  [ERROR] Chat ID not found: {data.get('description')}")
        print("  Fix: Send any message to your bot first, then run this again.")
        print(f"       Open Telegram → search '@{bot['username']}' → send /start")
        sys.exit(1)
except Exception as e:
    print(f"  [ERROR] Chat check failed: {e}")
    sys.exit(1)

# ── Step 3: Send test message ─────────────────────────────
print("\n[3/3] Sending test message to your Telegram...")
try:
    msg = (
        "✅ *TradingBot Telegram Test*\n\n"
        "If you received this message, your Telegram connection is working correctly.\n\n"
        "You will receive daily reminders at *8:30 AM* and trade alerts throughout the session."
    )
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": msg,
            "parse_mode": "Markdown"
        },
        timeout=10
    )
    data = r.json()
    if data.get("ok"):
        print("  [OK] Message sent successfully!")
        print("       Check your Telegram now.")
    else:
        print(f"  [ERROR] Message failed: {data.get('description')}")
        if "chat not found" in str(data.get("description", "")).lower():
            print(f"  Fix: Open Telegram → search the bot → send /start first.")
except Exception as e:
    print(f"  [ERROR] Send failed: {e}")
    sys.exit(1)

print("\n" + "="*55)
print("  All checks passed. Telegram is working.")
print("="*55 + "\n")
