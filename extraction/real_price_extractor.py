from PIL import Image
import pytesseract
import cv2
import re

# =========================
# TESSERACT PATH
# =========================

pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# =========================
# IMAGE PATH
# =========================

IMAGE_PATH = r"C:\Users\siddh\Downloads\HK\TradingBot\screenshots\2026-05-15"

IMAGE_FILE = "Nifty50_2026-05-15_13-53-43.png"

FULL_PATH = f"{IMAGE_PATH}\\{IMAGE_FILE}"

# =========================
# LOAD IMAGE
# =========================

image = cv2.imread(FULL_PATH)

# =========================
# CROP TOP BAR
# =========================

cropped = image[0:120, 0:900]

cv2.imwrite("price_debug.png", cropped)

# =========================
# OCR
# =========================

text = pytesseract.image_to_string(cropped)

print("========== OCR ==========")
print(text)
print("=========================")

# =========================
# FIND PRICE VALUES
# =========================

numbers = re.findall(r'[\d,]+\.\d+', text)

print("Detected Numbers:")
print(numbers)

# =========================
# TRY EXTRACTING CURRENT PRICE
# =========================

try:

    # Usually current price is near beginning
    current_price = float(numbers[0].replace(",", ""))

    print(f"\nLIVE CURRENT PRICE: {current_price}")

except Exception as e:

    print("Extraction Error:", e)