from playwright.sync_api import sync_playwright
from datetime import datetime
import os
import time

BASE_FOLDER = r"C:\Users\siddh\Downloads\HK\TradingBot\screenshots"

today = datetime.now().strftime("%Y-%m-%d")
today_folder = os.path.join(BASE_FOLDER, today)

os.makedirs(today_folder, exist_ok=True)

print("Screenshot folder ready:", today_folder)

with sync_playwright() as p:

    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")

    context = browser.contexts[0]

    page = context.pages[0]

    print("Connected to existing TradingView browser.")

    while True:

        timestamp = datetime.now().strftime("%H-%M-%S")

        filename = f"Nifty50_{timestamp}.png"

        filepath = os.path.join(today_folder, filename)

        page.screenshot(path=filepath)

        print("Saved:", filename)

        time.sleep(300)