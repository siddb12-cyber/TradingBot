"""
runtime/telegram_reminder.py
=============================
Daily 8:30 AM morning reminder sent to Telegram before TradingBot auto-starts.
Called by the Cowork scheduled task every weekday morning.

Usage:
    python -m runtime.telegram_reminder
"""

import sys
import requests
from datetime import datetime

# ── Credentials ───────────────────────────────────────────
BOT_TOKEN = "8861580303:AAHf_Y2yljKN9EEAkaz9_pcpZE6Aeanb6e4"
CHAT_ID   = "8331670846"

# ── Message ───────────────────────────────────────────────
def send_morning_reminder() -> bool:
    now   = datetime.now()
    today = now.strftime("%A, %d %b %Y")

    message = (
        f"🟢 *TradingBot — Morning Reminder*\n"
        f"📅 {today}\n\n"
        f"System auto-starts in *15 minutes* (08:45 AM).\n\n"
        f"✅ Make sure your laptop is *on and logged in*.\n"
        f"📊 NIFTY chart and Groww F\\&O will open automatically.\n"
        f"🔔 You'll receive trade alerts here throughout the session.\n\n"
        f"_Market opens: 09:15 AM | Last entry: 03:20 PM_"
    )

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id":    CHAT_ID,
                "text":       message,
                "parse_mode": "MarkdownV2",
            },
            timeout=10,
        )
        result = r.json()
        if result.get("ok"):
            print(f"[OK] Morning reminder sent at {now.strftime('%H:%M:%S')}")
            return True
        else:
            # Fallback: try plain text if MarkdownV2 fails
            r2 = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": f"TradingBot starting in 15 minutes. {today}"},
                timeout=10,
            )
            print(f"[FALLBACK] Sent plain text: {r2.json().get('ok')}")
            return r2.json().get("ok", False)
    except Exception as e:
        print(f"[ERROR] Telegram reminder failed: {e}")
        return False


if __name__ == "__main__":
    success = send_morning_reminder()
    sys.exit(0 if success else 1)
