import os
from dotenv import load_dotenv
import requests

load_dotenv()

token = os.getenv("BOT_TOKEN", "")
chat  = os.getenv("CHAT_ID", "")

print(f"BOT_TOKEN : {token[:15]}..." if token else "BOT_TOKEN : NOT SET")
print(f"CHAT_ID   : {chat}" if chat else "CHAT_ID   : NOT SET")

if not token or not chat:
    print("\nERROR: Missing BOT_TOKEN or CHAT_ID in .env file")
    exit(1)

print("\nSending test message to Telegram...")
r = requests.post(
    f"https://api.telegram.org/bot{token}/sendMessage",
    json={"chat_id": chat, "text": "TradingBot Telegram test - working!"},
    timeout=10
)
data = r.json()
print(f"Status : {r.status_code}")
print(f"Result : {data}")

if data.get("ok"):
    print("\nSUCCESS - Telegram is working correctly")
else:
    print(f"\nFAILED - Error: {data.get('description', 'unknown')}")
