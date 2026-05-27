from playwright.sync_api import sync_playwright
import time

print("Starting browser...")

with sync_playwright() as p:

    browser = p.chromium.launch(
        headless=False,
        slow_mo=500
    )

    page = browser.new_page()

    print("Opening Nifty chart...")

    page.goto(
        "https://www.tradingview.com/chart/?symbol=NSE%3ANIFTY",
        wait_until="domcontentloaded"
    )

    time.sleep(5)

    print("Setting 5-minute timeframe...")

    page.keyboard.press("5")

    time.sleep(1)

    page.keyboard.press("Enter")

    print("5-minute chart activated.")

    time.sleep(120)

    browser.close()