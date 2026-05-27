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

Priority 5 additions (current):
    - Trailing SL: after T1 hit → SL moves to breakeven (0 pts)
                   after T2 hit → SL moves to +T1_POINTS (locks in T1 profit)
    - Financial details in all Telegram messages:
        lots traded, capital at risk (Rs.), current P&L (Rs.)

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
    TEMP_DIR, TRADE_LOG_DIR, TRACKER_INTERVAL_SECONDS,
    STOP_LOSS_POINTS, TARGET_1_POINTS, TARGET_2_POINTS, TARGET_3_POINTS,
    NIFTY_LOT_SIZE, OPTION_DELTA,
)
from extraction.ocr_engine import extract_live_price
from core.trade_state import TradeStateManager
from core.market_hours import (
    is_market_open,
    is_eod_close_time,
    log_market_closed_reason,
)
from core.risk_engine import RiskEngine
from analytics.weekly_report import generate_and_send as generate_weekly_report

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
# FINANCIAL HELPERS
# =========================

def calc_pnl_inr(points: float, lots: int) -> float:
    """Convert NIFTY index points to approximate INR P&L across all lots."""
    return round(points * OPTION_DELTA * NIFTY_LOT_SIZE * lots, 2)


def calc_capital_at_risk(lots: int) -> float:
    """Maximum capital at risk (Rs.) if original SL is hit — before any trailing."""
    return round(STOP_LOSS_POINTS * OPTION_DELTA * NIFTY_LOT_SIZE * lots, 2)


# =========================
# POINTS + STATUS
# =========================

def calc_points(entry, live, direction):
    if direction == "CE":
        return live - entry
    elif direction == "PE":
        return entry - live
    return 0.0


def get_live_status(points: float, milestones_hit: list = None) -> str:
    """
    Determine current trade status with trailing SL logic.

    Trailing SL ladder (activated by milestones already hit):
        No milestone  : SL at -STOP_LOSS_POINTS  (e.g. -10 pts — original fixed stop)
        After T1 hit  : SL trails to 0            (breakeven — protect capital)
        After T2 hit  : SL trails to +TARGET_1_POINTS (e.g. +15 pts — lock T1 profit)

    Args:
        points:        Current unrealised P&L in NIFTY index points.
        milestones_hit: List of milestone status strings already recorded for this trade.

    Returns:
        One of TradeStateManager status constants (SL_HIT, TARGET1_HIT, etc., or OPEN).
    """
    milestones_hit = milestones_hit or []

    # Determine effective SL threshold based on milestones
    if TradeStateManager.TARGET2_HIT in milestones_hit:
        # T2 was hit — SL trails up to lock in T1 profit
        effective_sl = TARGET_1_POINTS          # e.g. +15 pts
    elif TradeStateManager.TARGET1_HIT in milestones_hit:
        # T1 was hit — SL trails to breakeven
        effective_sl = 0
    else:
        # No milestone yet — original fixed stop loss (below entry)
        effective_sl = -STOP_LOSS_POINTS        # e.g. -10 pts

    # Evaluate in priority order: SL → T3 → T2 → T1 → OPEN
    if points <= effective_sl:
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

def build_active_msg(signal, entry, live, points, trade_id, lots):
    """Initial 'trade is now active' alert with financial snapshot."""
    arrow       = "UP" if points >= 0 else "DOWN"
    pnl_inr     = calc_pnl_inr(points, lots)
    at_risk_inr = calc_capital_at_risk(lots)
    pnl_sign    = "+" if pnl_inr >= 0 else ""
    sep         = "--" * 16
    return (
        "TRADE ACTIVE\n" + sep + "\n"
        "Trade      : " + signal + "\n"
        "Entry      : " + "{:.2f}".format(entry) + "\n"
        "Live       : " + "{:.2f}".format(live) + "\n"
        "Move       : " + arrow + " " + "{:.2f}".format(abs(points)) + " pts\n"
        "Trade ID   : " + trade_id + "\n" + sep + "\n"
        "Lots       : " + str(lots) + "\n"
        "Capital Risk : Rs." + "{:.0f}".format(at_risk_inr) + "\n"
        "P&L (Live) : Rs." + pnl_sign + "{:.0f}".format(pnl_inr) + "\n"
        "[Paper Trading Mode]"
    )


def build_milestone_msg(signal, entry, live, points, milestone, trade_id,
                        milestones_hit, lots):
    """T1 or T2 milestone alert showing current financial position."""
    labels = {
        TradeStateManager.TARGET1_HIT: (
            "TARGET 1 HIT (+" + str(TARGET_1_POINTS) + " pts) "
            "-- SL now trails to BREAKEVEN"
        ),
        TradeStateManager.TARGET2_HIT: (
            "TARGET 2 HIT (+" + str(TARGET_2_POINTS) + " pts) "
            "-- SL now locks in T1 profit (+" + str(TARGET_1_POINTS) + " pts)"
        ),
    }
    label       = labels.get(milestone, milestone)
    arrow       = "UP" if points >= 0 else "DOWN"
    pnl_inr     = calc_pnl_inr(points, lots)
    pnl_sign    = "+" if pnl_inr >= 0 else ""
    sep         = "--" * 16
    return (
        "MILESTONE ALERT\n" + sep + "\n"
        "Trade      : " + signal + "\n"
        "Entry      : " + "{:.2f}".format(entry) + "\n"
        "Live       : " + "{:.2f}".format(live) + "\n"
        "Move       : " + arrow + " " + "{:.2f}".format(abs(points)) + " pts\n" + sep + "\n" +
        label + "\n"
        "Trade ID   : " + trade_id + "\n" + sep + "\n"
        "Lots       : " + str(lots) + "\n"
        "P&L (Now)  : Rs." + pnl_sign + "{:.0f}".format(pnl_inr) + "\n"
        "Still monitoring -- SL & T3 active\n"
        "[Paper Trading Mode]"
    )


def build_close_msg(signal, entry, exit_p, points, outcome, trade_id,
                    milestones, risk_summary, lots):
    """Trade closed (T3 or SL) with full financial breakdown."""
    arrow       = "UP" if points >= 0 else "DOWN"
    mstr        = " -> ".join(milestones) if milestones else "None"
    pnl_inr     = calc_pnl_inr(points, lots)
    at_risk_inr = calc_capital_at_risk(lots)
    pnl_sign    = "+" if pnl_inr >= 0 else ""
    daily_pnl_inr = round(
        risk_summary["daily_pnl_points"] * OPTION_DELTA * NIFTY_LOT_SIZE, 2
    )
    daily_sign  = "+" if daily_pnl_inr >= 0 else ""
    sep         = "--" * 16
    return (
        "TRADE CLOSED\n" + sep + "\n"
        "Trade      : " + signal + "\n"
        "Entry      : " + "{:.2f}".format(entry) + "\n"
        "Exit       : " + "{:.2f}".format(exit_p) + "\n"
        "Result     : " + arrow + " " + "{:.2f}".format(abs(points)) + " pts\n" + sep + "\n"
        "Outcome    : " + outcome + "\n"
        "Milestones : " + mstr + "\n"
        "Trade ID   : " + trade_id + "\n" + sep + "\n"
        "Lots       : " + str(lots) + "\n"
        "Capital Risk : Rs." + "{:.0f}".format(at_risk_inr) + "\n"
        "Trade P&L  : Rs." + pnl_sign + "{:.0f}".format(pnl_inr) + "\n" + sep + "\n"
        "Daily P&L  : " + str(risk_summary["daily_pnl_points"]) + " pts"
        "  (Rs." + daily_sign + "{:.0f}".format(daily_pnl_inr) + ")"
        "  |  Trades: " + str(risk_summary["trades_today"]) + "/" + str(risk_summary["max_trades"]) + "\n"
        "Trade closed. Ready for next signal.\n"
        "[Paper Trading Mode]"
    )


def build_eod_close_msg(signal, entry, exit_p, points, trade_id,
                        milestones, risk_summary, lots):
    """EOD force-close message with financial summary."""
    arrow       = "UP" if points >= 0 else "DOWN"
    mstr        = " -> ".join(milestones) if milestones else "None"
    pnl_inr     = calc_pnl_inr(points, lots)
    at_risk_inr = calc_capital_at_risk(lots)
    pnl_sign    = "+" if pnl_inr >= 0 else ""
    daily_pnl_inr = round(
        risk_summary["daily_pnl_points"] * OPTION_DELTA * NIFTY_LOT_SIZE, 2
    )
    daily_sign  = "+" if daily_pnl_inr >= 0 else ""
    sep         = "--" * 16
    return (
        "EOD TRADE CLOSED\n" + sep + "\n"
        "Trade      : " + signal + "\n"
        "Entry      : " + "{:.2f}".format(entry) + "\n"
        "Exit       : " + "{:.2f}".format(exit_p) + "\n"
        "Result     : " + arrow + " " + "{:.2f}".format(abs(points)) + " pts\n" + sep + "\n"
        "Outcome    : EOD CLOSE (market closing)\n"
        "Milestones : " + mstr + "\n"
        "Trade ID   : " + trade_id + "\n" + sep + "\n"
        "Lots       : " + str(lots) + "\n"
        "Capital Risk : Rs." + "{:.0f}".format(at_risk_inr) + "\n"
        "Trade P&L  : Rs." + pnl_sign + "{:.0f}".format(pnl_inr) + "\n" + sep + "\n"
        "Daily P&L  : " + str(risk_summary["daily_pnl_points"]) + " pts"
        "  (Rs." + daily_sign + "{:.0f}".format(daily_pnl_inr) + ")"
        "  |  Trades: " + str(risk_summary["trades_today"]) + "/" + str(risk_summary["max_trades"]) + "\n"
        "Market closing at 15:30. Trade force-closed.\n"
        "[Paper Trading Mode]"
    )


# =========================
# WEEKLY REPORT TRIGGER
# =========================

def _maybe_trigger_weekly_report(now: datetime, fired_dates: set) -> set:
    """
    Fire the weekly report generator once on Friday after 15:30 IST.

    The fired_dates set (in-memory) prevents duplicate runs within the
    same process session.  A sentinel file is also written so that a
    process restart on the same Friday does not re-send the report.

    Parameters
    ----------
    now         : Current datetime (must be IST — system clock set to IST)
    fired_dates : Set of date strings ("YYYY-MM-DD") already processed

    Returns
    -------
    Updated fired_dates set.
    """
    # Only on Fridays (weekday 4) at or after 15:30 IST
    if now.weekday() != 4:
        return fired_dates
    if not (now.hour == 15 and now.minute >= 30):
        return fired_dates

    today_str  = now.strftime("%Y-%m-%d")
    if today_str in fired_dates:
        return fired_dates   # Already run this session

    # Sentinel file: prevents re-run after process restart on same Friday
    sentinel = TRADE_LOG_DIR / "weekly" / f"weekly_report_sent_{today_str}.flag"
    if sentinel.exists():
        fired_dates.add(today_str)
        logger.debug("[WEEKLY] Already sent today (%s) — skipping.", today_str)
        return fired_dates

    logger.info("[WEEKLY] Friday 15:30+ IST — generating weekly report...")
    try:
        report_path = generate_weekly_report(ref_date=now)
        if report_path:
            logger.info("[WEEKLY] Report generated: %s", report_path.name)
            # Write sentinel to prevent duplicate runs
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.touch()
        else:
            logger.warning("[WEEKLY] Report generation returned None — check logs for errors.")
    except Exception as exc:
        logger.error("[WEEKLY] Report generation failed: %s", exc)

    fired_dates.add(today_str)
    return fired_dates


# =========================
# MAIN LOOP
# =========================

def run():
    logger.info("=" * 52)
    logger.info("LIVE TRADE TRACKER -- STARTING")
    logger.info("Tracker interval : " + str(TRACKER_INTERVAL_SECONDS) + "s")
    logger.info("Market hours     : 09:15-15:30 IST (Mon-Fri)")
    logger.info("EOD auto-close   : 15:29 IST")
    logger.info("Trailing SL      : T1 → breakeven | T2 → lock T1 profit")
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

        # In-memory set of dates for which weekly report was already fired
        _weekly_fired: set = set()

        while True:

            now       = datetime.now()
            timestamp = now.strftime("%H:%M:%S")
            logger.info("--" * 22)
            logger.info("[TRACKER] Tick | " + timestamp)

            # =========================
            # WEEKLY REPORT TRIGGER (Friday ≥ 15:30 IST)
            # Runs after market close — checked every tick so it fires
            # regardless of whether an active trade is open.
            # =========================

            _weekly_fired = _maybe_trigger_weekly_report(now, _weekly_fired)

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

                # Update risk engine daily state (pass direction for smart cooldown override)
                trade_direction = "BULLISH" if "CE" in signal else "BEARISH"
                risk.record_trade_closed(eod_outcome, points_result, direction=trade_direction)
                rs   = risk.get_summary()
                lots = rs.get("suggested_lots", 1)

                # Telegram EOD alert
                msg = build_eod_close_msg(
                    signal=      signal,
                    entry=       entry_price,
                    exit_p=      exit_price,
                    points=      points_result,
                    trade_id=    trade_id,
                    milestones=  milestones,
                    risk_summary=rs,
                    lots=        lots,
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

            # --- Financial context (lots, capital at risk) ---
            risk_summary = risk.get_summary()
            lots         = risk_summary.get("suggested_lots", 1)

            logger.info(
                "[TRACKER] Monitoring | id=" + trade_id +
                " | entry=" + "{:.2f}".format(entry_price) +
                " | lots=" + str(lots) +
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
            live_status = get_live_status(points, milestones)   # trailing SL aware

            logger.info(
                "[TRACKER] points=" + "{:+.2f}".format(points) +
                "  pnl=Rs." + "{:+.0f}".format(calc_pnl_inr(points, lots)) +
                "  status=" + live_status
            )

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

                # Update risk engine (pass direction for smart cooldown override)
                trade_direction = "BULLISH" if "CE" in signal else "BEARISH"
                risk.record_trade_closed(outcome, points_result, direction=trade_direction)
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
                    lots=        lots,
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
                        lots=          lots,
                    )
                    print(msg)
                    send_telegram(msg)
                    logger.info("[TELEGRAM] Milestone alert: " + live_status + " | " + trade_id)

            else:
                # --- ACTIVE: no change ---
                if last_sent is None:
                    msg = build_active_msg(signal, entry_price, live_price, points, trade_id, lots)
                    print(msg)
                    send_telegram(msg)
                    state.set_last_tracker_result(TradeStateManager.OPEN)
                    logger.info("[TELEGRAM] Initial active alert | " + trade_id)
                else:
                    logger.debug(
                        "[TRACKER] OPEN -- pts=" + "{:+.2f}".format(points) +
                        " pnl=Rs." + "{:+.0f}".format(calc_pnl_inr(points, lots)) +
                        " | last_sent=" + str(last_sent)
                    )

            time.sleep(TRACKER_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
