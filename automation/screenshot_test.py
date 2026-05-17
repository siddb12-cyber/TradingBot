import pyautogui
import time

print("Taking screenshot in 5 seconds...")
time.sleep(5)

screenshot = pyautogui.screenshot()

screenshot.save("nifty_chart.png")

print("Screenshot saved successfully.")