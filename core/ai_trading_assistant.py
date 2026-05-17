"""
core/ai_trading_assistant.py
============================
Primary signal engine. Checks trade state, market hours, and risk limits
before every scan.

Priority 4 additions:
    - RiskEngine integration: trade validation before open_trade()
    - Position sizing: lots + max loss included in Telegram signal
    - record_trade_opened() called after successful open_trade()

Priority 5 additions (Multi-Timeframe Confirmation):
    - MultiTimeframeAnalyzer replaces single-TF decide_signal()
    - Scans 5m, 15m, 1h timeframes per cycle via TradingView Playwright switching
    - Confidence score (0-100) computed from TF alignment + VWAP distance + EMA align
    - Trade gate: LOW confidence (<45) rejects trade with Telegram alert
    - MEDIUM (45-69) and HIGH (>=70) confidence proceed to risk check
    - Telegram signal includes: TF alignment, score, confidence level per TF breakdown

Paper Trading Only. No real broker execution.
"""

import logging
import time
from datetime import datetime
from typing import Optional

import pandas as pd
import requests
from playwright.sync_api import sync_playwright

from config.config import (
    configure_logging,
    BOT_TOKEN, CHAT_ID, CHROME_DEBUG_URL,
    SCREENSHOT_DIR, TRADE_LOG_DIR, SCAN_INTERVAL_SECONDS,
    NIFTY_STRIKE_INTERVAL, STOP_LOSS_POINTS,
    TARGET_1_POINTS, TARGET_2_POINTS, TARGET_3_POINTS,
    TIMEFRAMES, PRIMARY_TIMEFRAME,
    CONFIDENCE_HIGH_THRESHOLD, CONFIDENCE_MED_THRESHOLD,
)
from extraction.ocr_engine import extract_market_values
from core.trade_state import TradeStateManager
from core.market_hours import (
    is_market_open,
    seconds_until_next_open,
    log_market_closed_reason,
)
from core.risk_engine import RiskEngine
from core.multi_timeframe import (
    MultiTimeframeAnalyzer,
    CONFIDENCE_HIGH,
    CONFIDENCE_MEDIUM,
    CONFIDENCE_LOW,
)

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
# TRADE LOG
# =========================

def append_trade_log(trade_row):
    today    = datetime.now().strftime("%Y-%m-%d")
    log_file = TRADE_LOG_DIR / ("trade_log_" + today + ".xlsx")
    df_new   = pd.DataFrame([trade_row])
    try:
        if log_file.exists():
            df_existing = pd.read_excel(log_file, engine="openpyxl")
            pd.concat([df_existing, df_new], ignore_index=True).to_excel(
                log_file, index=False, engine="openpyxl"
            )
        else:
            df_new.to_excel(log_file, index=False, engine="openpyxl")
        logger.info("[LOG] Entry appended -> " + log_file.name)
    except Exception as e:
        logger.error(f"[LOG] Write failed: {e}")


# =========================
# TELEGRAM MESSAGES
# =========================

def _tf_breakdown_lines(timeframe_data: dict) -> str:
    """
    Build a compact per-TF breakdown string for Telegram messages.
    Example:
        5m  : BULLISH (P=24100 V=24050 E=24080)
        15m : BULLISH (P=24090 V=24030 E=24060)
        1h  : SIDEWAYS (OCR failed)
    """
    lines = []
    for tf in TIMEFRAMES:
        data = timeframe_data.get(tf, {})
        if not data.get("valid"):
            err = data.get("error", "unknown")
            lines.append(f"  {tf:<4}: N/A ({err[:30]})")
            continue
        d   = data.get("direction", "?")
        p   = data.get("price")
        v   = data.get("vwap")
        e   = data.get("ema9")
        p_s = f"{p:.0f}" if p else "?"
        v_s = f"{v:.0f}" if v else "?"
        e_s = f"{e:.0f}" if e else "?"
        lines.append(f"  {tf:<4}: {d:<9} (P={p_s} V={v_s} E={e_s})")
    return "\n".join(lines)


def build_signal_msg(ts, sig, trade_id, sizing, risk_summary):
    """
    Build Telegram signal alert — includes MTF alignment + confidence score.
    sig dict contains all MTF fields merged in by MultiTimeframeAnalyzer.
    """
    sep        = "--" * 16
    tf_lines   = _tf_breakdown_lines(sig.get("timeframe_data", {}))
    align_sum  = sig.get("alignment_summary", "?")
    score      = sig.get("confidence_score", 0)
    level      = sig.get("confidence_level", "?")

    # Primary TF data for header
    primary_data = sig.get("timeframe_data", {}).get(PRIMARY_TIMEFRAME, {})
    price = primary_data.get("price", 0)
    vwap  = primary_data.get("vwap", 0)
    ema9  = primary_data.get("ema9", 0)

    return (
        "NIFTY AI TRADE SIGNAL\n" + sep + "\n"
        "Time        : " + ts + "\n"
        "Trend       : " + sig["trend"] + "\n"
        "Entry Price : " + ("{:.2f}".format(price) if price else "?") + "\n"
        "VWAP (5m)   : " + ("{:.2f}".format(vwap) if vwap else "?") + "\n"
        "EMA9 (5m)   : " + ("{:.2f}".format(ema9) if ema9 else "?") + "\n" + sep + "\n"
        "Signal      : " + sig["trade_signal"] + "\n"
        "Stop Loss   : " + sig["stop_loss"] + "\n"
        "Target 1    : " + sig["target1"] + "\n"
        "Target 2    : " + sig["target2"] + "\n"
        "Target 3    : " + sig["target3"] + "\n" + sep + "\n"
        "MTF Alignment  : " + align_sum + "\n"
        "Timeframes  :\n" + tf_lines + "\n" + sep + "\n"
        "Confidence  : " + str(score) + "/100  [" + level + "]\n" + sep + "\n"
        "Qty (Lots)  : " + str(sizing["lots"]) + " lot(s)\n"
        "Max Loss    : Rs." + str(sizing["max_loss_inr"]) + " (" + str(sizing["risk_pct"]) + "% capital)\n"
        "Trade No.   : " + str(risk_summary["trades_today"]) + "/" + str(risk_summary["max_trades"]) + " today\n" + sep + "\n"
        "Trade ID    : " + trade_id + "\n"
        "[Paper Trading Mode]"
    )


def build_low_confidence_msg(ts, sig):
    """
    Sent when MTF confidence is LOW — trade blocked at confidence gate.
    """
    sep      = "--" * 16
    tf_lines = _tf_breakdown_lines(sig.get("timeframe_data", {}))
    score    = sig.get("confidence_score", 0)
    level    = sig.get("confidence_level", "LOW")
    align    = sig.get("alignment_summary", "?")

    return (
        "SIGNAL BLOCKED -- LOW CONFIDENCE\n" + sep + "\n"
        "Time       : " + ts + "\n"
        "Trend      : " + sig.get("trend", "?") + "\n"
        "Signal     : " + sig.get("trade_signal", "?") + "\n" + sep + "\n"
        "MTF Align  : " + align + "\n"
        "Timeframes :\n" + tf_lines + "\n" + sep + "\n"
        "Confidence : " + str(score) + "/100  [" + level + "]\n"
        "Threshold  : >= " + str(CONFIDENCE_MED_THRESHOLD) + " required\n" + sep + "\n"
        "No trade opened. Waiting for stronger alignment.\n"
        "[Paper Trading Mode]"
    )


def build_risk_rejected_msg(reason, sig_trend, score, level):
    sep = "--" * 16
    return (
        "TRADE BLOCKED -- RISK LIMIT\n" + sep + "\n"
        "Signal     : " + sig_trend + "\n"
        "Confidence : " + str(score) + "/100  [" + level + "]\n"
        "Reason     : " + reason + "\n" + sep + "\n"
        "No trade opened. Risk rules active.\n"
        "[Paper Trading Mode]"
    )


def build_sideways_msg(ts, tf_data, align_sum, score, level):
    sep      = "--" * 16
    tf_lines = _tf_breakdown_lines(tf_data)
    return (
        "NIFTY SCAN -- NO SIGNAL\n" + sep + "\n" +
        ts + "  |  MTF: " + align_sum + "\n"
        "Timeframes :\n" + tf_lines + "\n" + sep + "\n"
        "Confidence : " + str(score) + "/100  [" + level + "]\n"
        "Bias: SIDEWAYS -- watching\n"
        "[Paper Trading Mode]"
    )


# =========================
# MAIN LOOP
# =========================

def run():
    logger.info("=" * 52)
    logger.info("AI TRADING ASSISTANT -- STARTING")
    logger.info("Scan interval : " + str(SCAN_INTERVAL_SECONDS) + "s")
    logger.info("Market hours  : 09:15-15:30 IST (Mon-Fri)")
    logger.info("MTF timeframes: " + str(TIMEFRAMES))
    logger.info("Confidence    : HIGH>=" + str(CONFIDENCE_HIGH_THRESHOLD) +
                " MED>=" + str(CONFIDENCE_MED_THRESHOLD))
    logger.info("=" * 52)

    state    = TradeStateManager()
    risk     = RiskEngine()
    analyzer = MultiTimeframeAnalyzer()

    if state.has_active_trade():
        logger.info(
            "[ASSISTANT] Restarted with active trade: " +
            str(state.state["trade_id"]) + " / " + str(state.state["signal"])
        )

    # Log startup risk summary
    rs = risk.get_summary()
    logger.info(
        "[RISK] Session start | trades=" + str(rs["trades_today"]) +
        "/" + str(rs["max_trades"]) +
        " | pnl=" + str(rs["daily_pnl_points"]) + "pts" +
        " | consec_losses=" + str(rs["consecutive_losses"])
    )

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
        # SIGNAL LOOP
        # =========================

        # last_sent_trend: sideways alerts only fire on trend change.
        # Reset to None on trade open so post-close scan always alerts.
        last_sent_trend: Optional[str] = None

        while True:

            now       = datetime.now()
            today     = now.strftime("%Y-%m-%d")
            timestamp = now.strftime("%H:%M:%S")

            logger.info("--" * 22)
            logger.info("[LOOP] " + today + " " + timestamp)

            # =========================
            # STEP 0: MARKET HOURS GUARD
            # =========================

            if not is_market_open(now):
                log_market_closed_reason(now)
                wait_secs = min(seconds_until_next_open(now), SCAN_INTERVAL_SECONDS)
                logger.info("[ASSISTANT] Sleeping " + str(wait_secs) + "s (market closed)...")
                time.sleep(wait_secs)
                continue

            # =========================
            # STEP 1: STATE CHECK
            # =========================

            state.reload()

            if state.has_active_trade():
                a = state.state
                logger.info(
                    "[ASSISTANT] Trade active -- skipping scan. id=" +
                    str(a["trade_id"]) + " status=" + str(a["status"])
                )
                time.sleep(SCAN_INTERVAL_SECONDS)
                continue

            logger.info("[ASSISTANT] State=IDLE -- running MTF scan")

            # =========================
            # STEP 2: SCREENSHOT FOLDER
            # =========================

            today_folder = SCREENSHOT_DIR / today
            today_folder.mkdir(parents=True, exist_ok=True)
            ts_file = now.strftime("%H-%M-%S")

            # =========================
            # STEP 3: MULTI-TIMEFRAME ANALYSIS
            # MTF analyzer handles its own screenshots per timeframe.
            # =========================

            mtf = analyzer.analyze(page, today_folder, ts_file)

            if not mtf["valid"]:
                logger.warning("[MTF] Analysis invalid: " + str(mtf.get("error")))
                time.sleep(SCAN_INTERVAL_SECONDS)
                continue

            # Extract MTF result fields
            primary_direction = mtf["primary_direction"]
            sig               = mtf          # sig dict has all signal + MTF fields merged
            score             = mtf["confidence_score"]
            level             = mtf["confidence_level"]
            align_sum         = mtf["alignment_summary"]
            tf_data           = mtf["timeframe_data"]

            logger.info(
                "[MTF] direction=" + str(primary_direction) +
                " | score=" + str(score) +
                " | level=" + level +
                " | align=" + align_sum
            )

            # =========================
            # STEP 4A: SIDEWAYS CHECK — DEDUP
            # =========================

            if not mtf["is_trade"]:
                current_trend = mtf.get("trend", "SIDEWAYS")
                if current_trend == last_sent_trend:
                    logger.debug("[DEDUP] Trend unchanged -- suppressing sideways alert")
                else:
                    logger.info("[ASSISTANT] SIDEWAYS trend change -> sending alert")
                    send_telegram(build_sideways_msg(
                        ts=        timestamp,
                        tf_data=   tf_data,
                        align_sum= align_sum,
                        score=     score,
                        level=     level,
                    ))
                    last_sent_trend = current_trend
                time.sleep(SCAN_INTERVAL_SECONDS)
                continue

            # =========================
            # STEP 4B: CONFIDENCE GATE
            # LOW confidence → block trade, send alert, skip cycle.
            # MEDIUM and HIGH → proceed to risk check.
            # =========================

            if level == CONFIDENCE_LOW:
                logger.warning(
                    "[CONFIDENCE] Trade blocked — LOW confidence: " +
                    str(score) + "/100 | align=" + align_sum
                )
                send_telegram(build_low_confidence_msg(timestamp, sig))
                time.sleep(SCAN_INTERVAL_SECONDS)
                continue

            logger.info(
                "[CONFIDENCE] " + level + " confidence (" + str(score) +
                "/100) — proceeding to risk check"
            )

            # =========================
            # STEP 4C: RISK GATE
            # =========================

            risk.reload()
            risk_check = risk.check_trade_allowed()

            if not risk_check["allowed"]:
                logger.warning("[RISK] Trade rejected: " + risk_check["reason"])
                msg = build_risk_rejected_msg(
                    reason=    risk_check["reason"],
                    sig_trend= sig.get("trend", "?"),
                    score=     score,
                    level=     level,
                )
                print(msg)
                send_telegram(msg)
                time.sleep(SCAN_INTERVAL_SECONDS)
                continue

            # =========================
            # STEP 4D: POSITION SIZING
            # =========================

            sizing       = risk.calculate_position_size()
            risk_summary = risk.get_summary()

            logger.info(
                "[RISK] Sizing: lots=" + str(sizing["lots"]) +
                " | max_loss=Rs." + str(sizing["max_loss_inr"]) +
                " | risk=" + str(sizing["risk_pct"]) + "%"
            )

            # =========================
            # STEP 4E: OPEN TRADE
            # =========================

            # Resolve entry price from primary TF data
            primary_data = tf_data.get(PRIMARY_TIMEFRAME, {})
            entry_price  = primary_data.get("price", 0.0)
            vwap_entry   = primary_data.get("vwap", 0.0)
            ema9_entry   = primary_data.get("ema9", 0.0)

            try:
                trade_id = state.open_trade(
                    signal=      sig["trade_signal"],
                    entry_price= entry_price,
                    trend=       sig.get("trend", primary_direction),
                    vwap=        vwap_entry,
                    ema9=        ema9_entry,
                )
            except RuntimeError as e:
                logger.error("[ASSISTANT] open_trade rejected: " + str(e))
                time.sleep(SCAN_INTERVAL_SECONDS)
                continue

            # Record trade opened in risk engine
            risk.record_trade_opened()

            # Reset dedup so post-close scan fires a fresh alert
            last_sent_trend = None
            logger.info("[ASSISTANT] Trade OPENED: id=" + trade_id)

            # --- Telegram signal alert ---
            msg = build_signal_msg(timestamp, sig, trade_id, sizing, risk_summary)
            print(msg)
            send_telegram(msg)
            logger.info("[TELEGRAM] Signal sent: " + trade_id)

            # --- Excel entry row (includes confidence and MTF columns) ---
            trade_row = {
                "Date":            today,
                "Time":            timestamp,
                "Trend":           sig.get("trend", primary_direction),
                "Current Price":   entry_price,
                "VWAP":            vwap_entry,
                "EMA9":            ema9_entry,
                "Trade Signal":    sig["trade_signal"],
                "Stop Loss":       sig.get("stop_loss", ""),
                "Target 1":        sig.get("target1", ""),
                "Target 2":        sig.get("target2", ""),
                "Target 3":        sig.get("target3", ""),
                "Lots":            sizing["lots"],
                "Max Loss INR":    sizing["max_loss_inr"],
                "MTF Alignment":   align_sum,
                "Confidence Score": score,
                "Confidence Level": level,
                "TF 5m":           tf_data.get("5m", {}).get("direction", "N/A"),
                "TF 15m":          tf_data.get("15m", {}).get("direction", "N/A"),
                "TF 1h":           tf_data.get("1h", {}).get("direction", "N/A"),
                "Trade ID":        trade_id,
                "Trade Status":    TradeStateManager.OPEN,
                "Outcome":         None,
                "Points Result":   None,
                "Exit Price":      None,
                "Exit Time":       None,
            }
            append_trade_log(trade_row)
            logger.info("[LOG] Entry row written: trade_id=" + trade_id)

            logger.info("[LOOP] Done. Sleeping " + str(SCAN_INTERVAL_SECONDS) + "s...")
            time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
