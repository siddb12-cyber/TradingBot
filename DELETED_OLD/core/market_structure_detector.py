from playwright.sync_api import sync_playwright
from datetime import datetime
import pytesseract
from PIL import Image
import os
import time
import requests

from config import BOT_TOKEN, CHAT_ID

# =========================
# TESSERACT PATH
# =========================

pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# =========================
# TELEGRAM FUNCTION
# =========================

def send_telegram_message(message):

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": CHAT_ID,
        "text": message
    }

    requests.post(url, data=payload)

# =========================
# SCREENSHOT FOLDER
# =========================

BASE_FOLDER = r"C:\Users\siddh\Downloads\HK\TradingBot\screenshots"

# =========================
# START PLAYWRIGHT
# =========================

with sync_playwright() as p:

    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")

    context = browser.contexts[0]

    page = context.pages[0]

    print("Connected to TradingView")

    while True:

        now = datetime.now()

        today = now.strftime("%Y-%m-%d")
        timestamp = now.strftime("%H-%M-%S")

        today_folder = os.path.join(BASE_FOLDER, today)

        os.makedirs(today_folder, exist_ok=True)

        screenshot_path = os.path.join(
            today_folder,
            f"Nifty50_{today}_{timestamp}.png"
        )

        # =========================
        # TAKE SCREENSHOT
        # =========================

        page.screenshot(path=screenshot_path)

        print(f"Saved: {screenshot_path}")

        # =========================
        # OCR READ
        # =========================

        image = Image.open(screenshot_path)

        text = pytesseract.image_to_string(image)

        # =========================
        # SIMPLE STRUCTURE DETECTION
        # =========================

        trend = "SIDEWAYS"

        if "VWAP" in text and "EMA" in text:

            if "-" in text:
                trend = "BEARISH / PE BIAS"
            else:
                trend = "BULLISH / CE BIAS"

        message = f"""
NIFTY MARKET UPDATE

Time: {timestamp}
Trend: {trend}

Paper Trading Mode Active
"""

        print(message)

        send_telegram_message(message)

        # Wait 5 minutes
        time.sleep(300)