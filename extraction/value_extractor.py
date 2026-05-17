from PIL import Image
import pytesseract
import cv2

# =========================
# TESSERACT PATH
# =========================

pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# =========================
# IMAGE PATH
# =========================

IMAGE_PATH = r"C:\Users\siddh\Downloads\HK\TradingBot\screenshots\2026-05-15"

# Replace with latest screenshot filename
IMAGE_FILE = "Nifty50_2026-05-15_13-58-46.png"

FULL_PATH = f"{IMAGE_PATH}\\{IMAGE_FILE}"

# =========================
# LOAD IMAGE
# =========================

image = cv2.imread(FULL_PATH)

# =========================
# CROP TOP-LEFT AREA
# =========================

# Adjust if needed later
cropped = image[0:250, 0:700]

# Save cropped preview
cv2.imwrite("cropped_debug.png", cropped)

# =========================
# OCR READ
# =========================

text = pytesseract.image_to_string(cropped)

print("========== OCR OUTPUT ==========")
print(text)
print("================================")