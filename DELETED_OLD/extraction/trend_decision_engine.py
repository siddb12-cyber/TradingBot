from PIL import Image
import pytesseract
import cv2
import re
import requests

from config import BOT_TOKEN, CHAT_ID

# =========================
# TESSERACT PATH
# =========================

pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# =========================
# IMAGE PATH
# =========================

IMAGE_PATH = r"C:\Users\siddh\Downloads\HK\TradingBot\screenshots\2026-05-15"

# Replace with latest screenshot
IMAGE_FILE = "Nifty50_2026-05-15_13-53-43.png"

FULL_PATH = f"{IMAGE_PATH}\\{IMAGE_FILE}"

# =========================
# TELEGRAM FUNCTION
# =========================

def send_telegram_message(message):

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": CHAT_ID,
        "text": message
    }

    try:

        requests.post(url, data=payload)

    except Exception as e:

        print("Telegram Error:", e)

# =========================
# LOAD IMAGE
# =========================

image = cv2.imread(FULL_PATH)

# =========================
# CROP TOP AREA
# =========================

cropped = image[0:250, 0:700]

# Optional debug image
cv2.imwrite("cropped_debug.png", cropped)

# =========================
# OCR READ
# =========================

text = pytesseract.image_to_string(cropped)

print("========== OCR OUTPUT ==========")
print(text)
print("================================")

print("RAW OCR TEXT:")
print(text)

# =========================
# EXTRACT VALUES
# =========================

numbers = re.findall(r'[\d,]+\.\d+', text)

print("Extracted Numbers:", numbers)

# =========================
# BASIC LOGIC
# =========================

trend = "SIDEWAYS"

try:

    # OCR OUTPUT:
    # ['2.5', '23,763.97', '23,799.92', '23,728.0', '23,738.36']

    # 2.5 = Volume
    # 23,763.97 = VWAP
    # 23,738.36 = EMA9

    vwap = float(numbers[1].replace(",", ""))

    ema = float(numbers[4].replace(",", ""))

    # Temporary manual current price
    current_price = 23771

    print(f"VWAP: {vwap}")
    print(f"EMA9: {ema}")
    print(f"Current Price: {current_price}")

    # =========================
    # TREND DECISION
    # =========================

    if current_price > vwap and current_price > ema:

        trend = "BULLISH / CE BIAS"

    elif current_price < vwap and current_price < ema:

        trend = "BEARISH / PE BIAS"

    else:

        trend = "SIDEWAYS"

except Exception as e:

    print("Error:", e)

# =========================
# TELEGRAM ALERT
# =========================

message = f"""
NIFTY TREND ANALYSIS

Trend: {trend}

Current Price: {current_price}

VWAP: {vwap}
EMA9: {ema}

Paper Trading Mode
"""

print(message)

send_telegram_message(message)