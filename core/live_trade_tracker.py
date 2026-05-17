"""
core/live_trade_tracker.py
==========================
Live trade monitoring, milestone detection, and outcome writer.
Reads TradeStateManager every tick. Writes milestones and closes trades.

Priority 3 additions:
    - Market hours guard: only monitors Mon-Fri, 09:15-15:30 IST
    - EOD auto-close: at 15:29 IST, any open trade is force-closed

Priority 4 additions:
    - RiskEngine.record_trade_closed() called after every close (T3, SL, EOD)
      to update daily P&L, consecutive losses, and cooldown state

Paper Trading Only.
"""

import logging
import time
from datetime import datetime

import requests
from playwright.sync_api import sync_playwright

from config.config import (
    configure_logging,
    BOT_TOKEN, CHAT_ID, CHROME_DEBUG_URL,
    TEMP_DIR, TRACKER_INTERVAL_SECONDS,
    STOP_LOSS_POINTS, TARGET_1_POINTS, TARGET_2_POINTS, TARGET_3_POINTS,
)
from extraction.ocr_engine import extract_live_price
from core.trade_state import TradeStateManager
from core.market_hours import (
    is_market_open,
    is_eod_close_time,
    log_market_closed_reason,
)
from core.risk_engine import RiskEngine

# =========================
# LOGGING
# =========================

configure_logging()
logger = logging.getLogger(__name__)


# =========================
# TELEGRAM
# =========================

def send_telegram(message):
    url     = "https://api.telegram.org/bot" + BOT_TOKEN + "/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message}
    try:
        r = requests.post(url, data=payload, timeout=10)
        if r.status_code != 200:
            logger.warning("[TELEGRAM] " + str(r.status_code) + ": " + r.text[:200])
        else:
            logger.debug("[TELEGRAM] Sent")
    except requests.RequestException as e:
        logger.error(f"[TELEGRAM] Error: {e}")


# =========================
# POINTS + STATUS
# =========================

def calc_points(entry, live, direction):
    if direction == "CE":
        return live - entry
    elif direction == "PE":
        return entry - live
    return 0.0


def get_live_status(points):
    if points <= -STOP_LOSS_POINTS:
        return TradeStateManager.SL_HIT
    elif points >= TARGET_3_POINTS:
        return TradeStateManager.TARGET3_HIT
    elif points >= TARGET_2_POINTS:
        return TradeStateManager.TARGET2_HIT
    elif points >= TARGET_1_POINTS:
        return TradeStateManager.TARGET1_HIT
    return TradeStateManager.OPEN


# =========================
# TELEGRAM MESSAGES
# =========================

def build_milestone_msg(signal, entry, live, points, milestone, trade_id, milestones_hit):
    labels = {
        TradeStateManager.TARGET1_HIT: "TARGET 1 HIT (+" + str(TARGET_1_POINTS) + " pts)",
        TradeStateManager.TARGET2_HIT: "TARGET 2 HIT (+" + str(TARGET_2_POINTS) + " pts) -- watching T3",
    }
    label = labels.get(milestone, milestone)
    arrow = "UP" if points >= 0 else "DOWN"
    sep   = "--" * 16
    return (
        "MILESTONE ALERT\n" + sep + "\n"
        "Trade    : " + signal + "\n"
        "Entry    : " + "{:.2f}".format(entry) + "\n"
        "Live     : " + "{:.2f}".format(live) + "\n"
        "Move     : " + arrow + " " + "{:.2f}".format(abs(points)) + " pts\n" + sep + "\n" +
        label + "\n"
        "Trade ID : " + trade_id + "\n"
        "Still monitoring -- SL & T3 active\n"
        "[Paper Trading Mode]"
    )


def build_close_msg(signal, entry, exit_p, points, outcome, trade_id, milestones, risk_summary):
    arrow = "UP" if points >= 0 else "DOWN"
    mstr  = " -> ".join(milestones) if milestones else "None"
    sep   = "--" * 16
    return (
        "TRADE CLOSED\n" + sep + "\n"
        "Trade      : " + signal + "\n"
        "Entry      : " + "{:.2f}".format(entry) + "\n"
        "Exit       : " + "{:.2f}".format(exit_p) + "\n"
        "Result     : " + arrow + " " + "{:.2f}".format(abs(points)) + " pts\n" + sep + "\n"
        "Outcome    : " + outcome + "\n"
        "Milestones : " + mstr + "\n"
        "Trade ID   : " + trade_id + "\n" + sep + "\n"
        "Daily P&L  : " + str(risk_summary["daily_pnl_points"]) + " pts" +
        "  |  Trades: " + str(risk_summary["trades_today"]) + "/" + str(risk_summary["max_trades"]) + "\n"
        "Trade closed. Ready for next signal.\n"
        "[Paper Trading Mode]"
    )


def build_eod_close_msg(signal, entry, exit_p, points, trade_id, milestones, risk_summary):
    arrow = "UP" if points >= 0 else "DOWN"
    mstr  = " -> ".join(milestones) if milestones else "None"
    sep   = "--" * 16
    return (
        "EOD TRADE CLOSED\n" + sep + "\n"
        "Trade      : " + signal + "\n"
        "Entry      : " + "{:.2f}".format(entry) + "\n"
        "Exit       : " + "{:.2f}".format(exit_p) + "\n"
        "Result     : " + arrow + " " + "{:.2f}".format(abs(points)) + " pts\n" + sep + "\n"
        "Outcome    : EOD CLOSE (market closing)\n"
        "Milestones : " + mstr + "\n"
        "Trade ID   : " + trade_id + "\n" + sep + "\n"
        "Daily P&L  : " + str(risk_summary["daily_pnl_points"]) + " pts" +
        "  |  Trades: " + str(risk_summary["trades_today"]) + "/" + str(risk_summary["max_trades"]) + "\n"
        "Market closing at 15:30. Trade force-closed.\n"
        "[Paper Trading Mode]"
    )


def build_active_msg(signal, entry, live, points, trade_id):
    arrow = "UP" if points >= 0 else "DOWN"
    sep   = "--" * 16
    return (
        "TRADE ACTIVE\n" + sep + "\n"
        "Trade    : " + signal + "\n"
        "Entry    : " + "{:.2f}".format(entry) + "\n"
        "Live     : " + "{:.2f}".format(live) + "\n"
        "Move     : " + arrow + " " + "{:.2f}".format(abs(points)) + " pts\n"
        "Trade ID : " + trade_id + "\n"
        "[Paper Trading Mode]"
    )


# =========================
# MAIN LOOP
# =========================

def run():
    logger.info("=" * 52)
    logger.info("LIVE TRADE TRACKER -- STARTING")
    logger.info("Tracker interval : " + str(TRACKER_INTERVAL_SECONDS) + "s")
    logger.info("Market hours     : 09:15-15:30 IST (Mon-Fri)")
    logger.info("EOD auto-close   : 15:29 IST")
    logger.info("=" * 52)

    state = TradeStateManager()
    risk  = RiskEngine()

    if state.has_active_trade():
        a = state.state
        logger.info(
            "[TRACKER] Resuming trade: " + str(a["trade_id"]) + " / " +
            str(a["signal"]) + " / last_sent=" + str(a.get("last_tracker_result"))
        )
    else:
        logger.info("[TRACKER] No active trade. Waiting for signal...")

    with sync_playwright() as p:

        try:
            browser = p.chromium.connect_over_cdp(CHROME_DEBUG_URL)
            context = browser.contexts[0]
            page    = context.pages[0]
            logger.info("[PLAYWRIGHT] Connected to TradingView")
        except Exception as e:
            logger.critical(f"[PLAYWRIGHT] Cannot connect: {e}")
            return

        # =========================
        # TRACKER LOOP
        # =========================

        while True:

            now       = datetime.now()
            timestamp = now.strftime("%H:%M:%S")
            logger.info("--" * 22)
            logger.info("[TRACKER] Tick | " + timestamp)

            # =========================
            # STEP 0: MARKET HOURS GUARD
            # =========================

            if not is_market_open(now):
                log_market_closed_reason(now)
                logger.info("[TRACKER] Market closed -- sleeping " + str(TRACKER_INTERVAL_SECONDS) + "s")
                time.sleep(TRACKER_INTERVAL_SECONDS)
                continue

            # --- Reload state every tick ---
            state.reload()
            risk.reload()

            # =========================
            # STEP 1: EOD AUTO-CLOSE CHECK
            # =========================

            if is_eod_close_time(now) and state.has_active_trade():

                active      = state.get_active_trade()
                trade_id    = active["trade_id"]
                signal      = active["signal"]
                direction   = active["direction"]
                entry_price = float(active["entry_price"])
                milestones  = active.get("milestones_hit", [])

                logger.info("[TRACKER] EOD AUTO-CLOSE | id=" + trade_id)

                # --- Final price read ---
                screenshot_path = TEMP_DIR / "eod_close_temp.png"
                exit_price      = entry_price
                points_result   = 0.0

                try:
                    page.screenshot(path=str(screenshot_path))
                    live_price = extract_live_price(screenshot_path)
                    if live_price is not None:
                        exit_price    = live_price
                        points_result = round(calc_points(entry_price, live_price, direction), 2)
                        logger.info("EOD exit=" + "{:.2f}".format(exit_price) + " pts=" + "{:+.2f}".format(points_result))
                    else:
                        logger.warning("[TRACKER] EOD OCR failed -- using entry price (0 pts)")
                except Exception as e:
                    logger.error("[TRACKER] EOD screenshot failed: " + str(e))

                eod_outcome = TradeStateManager.OUTCOME_LABELS[TradeStateManager.EOD_CLOSE]

                # Close trade (writes Excel)
                state.close_trade(
                    exit_price=    exit_price,
                    outcome=       eod_outcome,
                    points_result= points_result,
                )

                # Update risk engine daily state
                risk.record_trade_closed(eod_outcome, points_result)
                rs = risk.get_summary()

                # Telegram EOD alert
                msg = build_eod_close_msg(
                    signal=    signal,
                    entry=     entry_price,
                    exit_p=    exit_price,
                    points=    points_result,
                    trade_id=  trade_id,
                    milestones=milestones,
                    risk_summary=rs,
                )
                print(msg)
                send_telegram(msg)
                logger.info("[TELEGRAM] EOD close alert sent: " + trade_id)

                logger.info("[TRACKER] EOD done. Sleeping 120s...")
                time.sleep(120)
                continue

            # =========================
            # STEP 2: NORMAL TICK
            # =========================

            if not state.has_active_trade():
                logger.info(
                    "[TRACKER] State=" + state.get_status() +
                    " -- no active trade. Sleeping " + str(TRACKER_INTERVAL_SECONDS) + "s..."
                )
                time.sleep(TRACKER_INTERVAL_SECONDS)
                continue

            # --- Trade details ---
            active      = state.get_active_trade()
            trade_id    = active["trade_id"]
            signal      = active["signal"]
            direction   = active["direction"]
            entry_price = float(active["entry_price"])
            milestones  = active.get("milestones_hit", [])
            last_sent   = state.get_last_tracker_result()

            logger.info(
                "[TRACKER] Monitoring | id=" + trade_id +
                " | entry=" + "{:.2f}".format(entry_price) +
                " | status=" + str(active["status"])
            )

            # --- Screenshot + OCR ---
            screenshot_path = TEMP_DIR / "live_tracker_temp.png"

            try:
                page.screenshot(path=str(screenshot_path))
            except Exception as e:
                logger.error("[SCREENSHOT] Failed: " + str(e) + ". Skipping tick.")
                time.sleep(TRACKER_INTERVAL_SECONDS)
                continue

            live_price = extract_live_price(screenshot_path)

            if live_price is None:
                logger.warning("[TRACKER] OCR failed -- skipping tick.")
                time.sleep(TRACKER_INTERVAL_SECONDS)
                continue

            logger.info("[TRACKER] live_price=" + "{:.2f}".format(live_price))

            points      = calc_points(entry_price, live_price, direction)
            live_status = get_live_status(points)

            logger.info("[TRACKER] points=" + "{:+.2f}".format(points) + "  status=" + live_status)

            # =========================
            # HANDLE STATUS
            # =========================

            if live_status in TradeStateManager.TERMINAL_STATUSES:

                # --- TERMINAL: T3 or SL ---
                outcome       = TradeStateManager.OUTCOME_LABELS[live_status]
                points_result = round(points, 2)

                logger.info("[TRACKER] TERMINAL: " + live_status + " | pts=" + "{:+.2f}".format(points_result))

                state.close_trade(
                    exit_price=    live_price,
                    outcome=       outcome,
                    points_result= points_result,
                )

                # Update risk engine
                risk.record_trade_closed(outcome, points_result)
                rs = risk.get_summary()

                msg = build_close_msg(
                    signal=      signal,
                    entry=       entry_price,
                    exit_p=      live_price,
                    points=      points_result,
                    outcome=     outcome,
                    trade_id=    trade_id,
                    milestones=  milestones,
                    risk_summary=rs,
                )
                print(msg)
                send_telegram(msg)
                logger.info("[TELEGRAM] Close alert sent: " + outcome + " | " + trade_id)

            elif live_status in (TradeStateManager.TARGET1_HIT, TradeStateManager.TARGET2_HIT):

                # --- MILESTONE: T1 or T2 ---
                if state.milestone_already_hit(live_status):
                    logger.debug("[TRACKER] " + live_status + " already recorded.")
                else:
                    logger.info("[TRACKER] MILESTONE: " + live_status)
                    state.update_milestone(live_status)

                    msg = build_milestone_msg(
                        signal=        signal,
                        entry=         entry_price,
                        live=          live_price,
                        points=        points,
                        milestone=     live_status,
                        trade_id=      trade_id,
                        milestones_hit=milestones,
                    )
                    print(msg)
                    send_telegram(msg)
                    logger.info("[TELEGRAM] Milestone alert: " + live_status + " | " + trade_id)

            else:
                # --- ACTIVE: no change ---
                if last_sent is None:
                    msg = build_active_msg(signal, entry_price, live_price, points, trade_id)
                    print(msg)
                    send_telegram(msg)
                    state.set_last_tracker_result(TradeStateManager.OPEN)
                    logger.info("[TELEGRAM] Initial active alert | " + trade_id)
                else:
                    logger.debug(
                        "[TRACKER] OPEN -- pts=" + "{:+.2f}".format(points) +
                        " | last_sent=" + str(last_sent)
                    )

            time.sleep(TRACKER_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
