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

Priority 6 additions (OI + News Sentiment):
    - OIAnalysis computes PCR, max pain -> score adjustment (-20 to +8)
    - NewsSentimentEngine fetches VIX, US Futures, RSS, Google News -> adjustment (-20 to +5)
    - Adjustments applied AFTER base MTF score, BEFORE confidence gate
    - Adjusted score clamped to 0-100
    - Telegram signal now includes OI and sentiment summary lines

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
    CONFIDENCE_VERY_HIGH_THRESHOLD, CONFIDENCE_HIGH_THRESHOLD, CONFIDENCE_MED_THRESHOLD,
    SCALE_UP_MULTIPLIER, SCALE_UP_MAX_LOTS,
    NIFTY_LOT_SIZE, OPTION_DELTA, ACCOUNT_CAPITAL,
    TRADINGVIEW_URL,
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
from core.oi_analysis import OIAnalysis
from core.news_sentiment import NewsSentimentEngine

# =========================
# LOGGING
# =========================

configure_logging()
logger = logging.getLogger(__name__)


# =========================
# BROWSER HELPERS
# =========================

def _page_is_alive(page) -> bool:
    """
    Return True if the Playwright page is still connected to its browser.
    Accessing page.url throws TargetClosedError when the browser was killed/relaunched.
    """
    if page is None:
        return False
    try:
        _ = page.url
        return True
    except Exception:
        return False


def _connect_and_navigate(p, chrome_url: str, tradingview_url: str, max_attempts: int = 10):
    """
    Connect to Chrome CDP, get the first page, and ensure it is showing the
    TradingView chart (navigates there if on a blank tab).

    Returns the ready page, or None if all attempts fail.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            browser = p.chromium.connect_over_cdp(chrome_url)
            context = browser.contexts[0]
            page    = context.pages[0]

            # If blank tab / wrong page, navigate to TradingView chart
            current = ""
            try:
                current = page.url
            except Exception:
                pass

            if "tradingview.com/chart" not in current:
                logger.info(
                    "[PLAYWRIGHT] Page is not on TradingView ('%s') — navigating...",
                    current[:60] if current else "unknown"
                )
                page.goto(tradingview_url, wait_until="domcontentloaded", timeout=45_000)
                page.wait_for_timeout(3_000)   # Let chart fully render

            logger.info("[PLAYWRIGHT] Connected to TradingView (attempt %d)", attempt)
            return page

        except Exception as e:
            logger.warning("[PLAYWRIGHT] Connect attempt %d failed: %s", attempt, e)
            time.sleep(5)

    logger.error("[PLAYWRIGHT] Could not connect to Chrome after %d attempts", max_attempts)
    return None


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


def build_signal_msg(ts, sig, trade_id, sizing, risk_summary, oi_result=None, sent_result=None):
    """
    Build Telegram signal alert -- includes MTF alignment + confidence score
    + OI analysis summary + news sentiment summary.
    sig dict contains all MTF fields merged in by MultiTimeframeAnalyzer.
    """
    sep        = "--" * 16
    tf_lines   = _tf_breakdown_lines(sig.get("timeframe_data", {}))
    align_sum  = sig.get("alignment_summary", "?")
    score      = sig.get("adjusted_confidence_score", sig.get("confidence_score", 0))
    base_score = sig.get("confidence_score", 0)
    level      = sig.get("confidence_level", "?")

    # Primary TF data for header
    primary_data = sig.get("timeframe_data", {}).get(PRIMARY_TIMEFRAME, {})
    price = primary_data.get("price", 0)
    vwap  = primary_data.get("vwap", 0)
    ema9  = primary_data.get("ema9", 0)

    # OI summary line
    if oi_result and oi_result.get("valid"):
        pcr_val  = oi_result.get("pcr")
        mp_val   = oi_result.get("max_pain")
        atm_bias = oi_result.get("atm_bias", "NEUTRAL")
        oi_adj_v = oi_result.get("score_adjustment", 0)
        pcr_str  = "PCR=%.2f" % pcr_val if pcr_val else "PCR=N/A"
        mp_str   = "MaxPain=%d" % int(mp_val) if mp_val else "MaxPain=N/A"
        oi_line  = "%s  %s  ATMbias=%s  (%+dpts)" % (pcr_str, mp_str, atm_bias, oi_adj_v)
    else:
        oi_line = "N/A (fetch failed)"

    # Sentiment summary line
    if sent_result:
        vix_v   = sent_result.get("vix")
        us_pct  = sent_result.get("us_futures_pct")
        mood    = sent_result.get("sentiment", "NEUTRAL")
        s_adj_v = sent_result.get("total_adjustment", 0)
        vix_str = "VIX=%.1f" % vix_v if vix_v else "VIX=N/A"
        fut_str = "ES=F=%+.2f%%" % us_pct if us_pct is not None else "ES=F=N/A"
        sent_line = "%s  %s  News=%s  (%+dpts)" % (vix_str, fut_str, mood, s_adj_v)
    else:
        sent_line = "N/A"

    # Score breakdown line
    oi_adj_val = (oi_result or {}).get("score_adjustment", 0)
    s_adj_val  = (sent_result or {}).get("total_adjustment", 0)
    score_line = "Base=%d  OI=%+d  Sent=%+d  Final=%d/100" % (
        base_score, oi_adj_val, s_adj_val, score
    )

    # Scale-up suggestion block (VERY HIGH confidence only)
    scale_block = ""
    if score >= CONFIDENCE_VERY_HIGH_THRESHOLD:
        import math
        std_lots    = sizing["lots"]
        scaled_lots = min(int(math.floor(std_lots * SCALE_UP_MULTIPLIER)), SCALE_UP_MAX_LOTS)
        sl_per_lot  = STOP_LOSS_POINTS * OPTION_DELTA * NIFTY_LOT_SIZE
        scaled_risk = round(scaled_lots * sl_per_lot, 0)
        scaled_risk_pct = round((scaled_lots * sl_per_lot / ACCOUNT_CAPITAL) * 100, 1)
        scale_block = (
            sep + "\n"
            "*** SCALE-UP SUGGESTION ***\n"
            "Score " + str(score) + "/100 — VERY HIGH conviction signal\n"
            "Standard : " + str(std_lots) + " lot(s)  →  Rs." + str(int(sizing["max_loss_inr"])) + " at risk\n"
            "Scaled   : " + str(scaled_lots) + " lot(s)  →  Rs." + str(int(scaled_risk)) +
            " at risk (" + str(scaled_risk_pct) + "% capital)\n"
            "All three timeframes aligned. Consider increasing size\n"
            "if you have high conviction in current market conditions.\n"
            "NOTE: Manual decision only — no auto-execution.\n"
        )

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
        "OI Analysis : " + oi_line + "\n"
        "Sentiment   : " + sent_line + "\n" + sep + "\n"
        "Confidence  : " + score_line + "  [" + level + "]\n" + sep + "\n"
        "Qty (Lots)  : " + str(sizing["lots"]) + " lot(s)\n"
        "Max Loss    : Rs." + str(sizing["max_loss_inr"]) + " (" + str(sizing["risk_pct"]) + "% capital)\n"
        "Trade No.   : " + str(risk_summary["trades_today"]) + "/" + str(risk_summary["max_trades"]) + " today\n" +
        scale_block + sep + "\n"
        "Trade ID    : " + trade_id + "\n"
        "[Paper Trading Mode]"
    )


def build_low_confidence_msg(ts, sig):
    """
    Sent when MTF confidence is LOW -- trade blocked at confidence gate.
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

    state     = TradeStateManager()
    risk      = RiskEngine()
    analyzer  = MultiTimeframeAnalyzer()
    oi_engine = OIAnalysis()
    sentiment = NewsSentimentEngine()

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

        page = _connect_and_navigate(p, CHROME_DEBUG_URL, TRADINGVIEW_URL)
        if page is None:
            logger.critical("[PLAYWRIGHT] Cannot connect to Chrome — aborting.")
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
            # STEP 2: BROWSER HEALTH CHECK
            # If the watchdog relaunched Chrome, the page object is stale.
            # Reconnect before every scan so we never analyse a dead page.
            # =========================

            if not _page_is_alive(page):
                logger.warning(
                    "[PLAYWRIGHT] Page connection lost (browser was relaunched). "
                    "Reconnecting..."
                )
                page = _connect_and_navigate(p, CHROME_DEBUG_URL, TRADINGVIEW_URL)
                if page is None:
                    logger.error(
                        "[PLAYWRIGHT] Could not reconnect — sleeping %ds before retry.",
                        SCAN_INTERVAL_SECONDS,
                    )
                    time.sleep(SCAN_INTERVAL_SECONDS)
                    continue
                # Allow TradingView to fully settle after a fresh connection
                logger.info("[PLAYWRIGHT] Reconnect OK — waiting 5s for chart to settle...")
                time.sleep(5)

            # =========================
            # STEP 3: SCREENSHOT FOLDER
            # =========================

            today_folder = SCREENSHOT_DIR / today
            today_folder.mkdir(parents=True, exist_ok=True)
            ts_file = now.strftime("%H-%M-%S")

            # =========================
            # STEP 4: MULTI-TIMEFRAME ANALYSIS
            # MTF analyzer handles its own screenshots per timeframe.
            # =========================

            try:
                mtf = analyzer.analyze(page, today_folder, ts_file)
            except Exception as _e:
                _err = str(_e)
                if "TargetClosedError" in type(_e).__name__ or "Target page" in _err or "browser has been closed" in _err:
                    # Browser died mid-scan — mark page dead, retry next cycle
                    logger.warning(
                        "[PLAYWRIGHT] Browser died during MTF analysis (%s). "
                        "Will reconnect on next cycle.", _err[:120]
                    )
                    page = None   # Forces reconnect at top of next scan cycle
                else:
                    logger.error("[ASSISTANT] Unexpected error in MTF analysis: %s", _e)
                time.sleep(SCAN_INTERVAL_SECONDS)
                continue

            if not mtf["valid"]:
                logger.warning("[MTF] Analysis invalid: " + str(mtf.get("error")))
                time.sleep(SCAN_INTERVAL_SECONDS)
                continue

            # Extract MTF result fields
            primary_direction = mtf["primary_direction"]
            sig               = mtf          # sig dict has all signal + MTF fields merged
            base_score        = mtf["confidence_score"]
            level             = mtf["confidence_level"]
            align_sum         = mtf["alignment_summary"]
            tf_data           = mtf["timeframe_data"]

            logger.info(
                "[MTF] direction=" + str(primary_direction) +
                " | base_score=" + str(base_score) +
                " | level=" + level +
                " | align=" + align_sum
            )

            # =========================
            # STEP 3A: OI + SENTIMENT ADJUSTMENTS
            # Apply after MTF base score, before confidence gate.
            # Both engines are non-blocking -- failure returns 0 adjustment.
            # =========================

            # Resolve entry price for OI ATM calculation
            primary_data_pre = tf_data.get(PRIMARY_TIMEFRAME, {})
            pre_price        = primary_data_pre.get("price") or 0.0

            oi_result   = None
            sent_result = None
            oi_adj      = 0
            sent_adj    = 0

            if mtf["is_trade"] and primary_direction in ("BULLISH", "BEARISH"):
                # OI adjustment
                try:
                    oi_result = oi_engine.get_score_adjustment(
                        current_price=pre_price,
                        signal_direction=primary_direction,
                    )
                    oi_adj = oi_result.get("score_adjustment", 0)
                    logger.info("[OI] Adjustment: %+d (PCR=%s ATMbias=%s)",
                                oi_adj,
                                "%.3f" % oi_result["pcr"] if oi_result.get("pcr") else "N/A",
                                oi_result.get("atm_bias", "N/A"))
                except Exception as e:
                    logger.warning("[OI] Engine error (non-fatal): %s", e)

                # Sentiment adjustment
                try:
                    sent_result = sentiment.get_score_adjustment(
                        signal_direction=primary_direction,
                    )
                    sent_adj = sent_result.get("total_adjustment", 0)
                    logger.info("[SENTIMENT] Adjustment: %+d (VIX=%s Futures=%s Mood=%s)",
                                sent_adj,
                                "%.1f" % sent_result["vix"] if sent_result.get("vix") else "N/A",
                                "%+.2f%%" % sent_result["us_futures_pct"] if sent_result.get("us_futures_pct") is not None else "N/A",
                                sent_result.get("sentiment", "N/A"))
                except Exception as e:
                    logger.warning("[SENTIMENT] Engine error (non-fatal): %s", e)

            # Apply adjustments and clamp to 0-100
            adjusted_score = max(0, min(100, base_score + oi_adj + sent_adj))
            sig["adjusted_confidence_score"] = adjusted_score

            # Recalculate confidence level from adjusted score
            if adjusted_score >= CONFIDENCE_HIGH_THRESHOLD:
                adjusted_level = CONFIDENCE_HIGH
            elif adjusted_score >= CONFIDENCE_MED_THRESHOLD:
                adjusted_level = CONFIDENCE_MEDIUM
            else:
                adjusted_level = CONFIDENCE_LOW

            sig["confidence_level"] = adjusted_level
            score = adjusted_score
            level = adjusted_level

            logger.info(
                "[SCORE] base=%d  oi=%+d  sentiment=%+d  adjusted=%d  level=%s",
                base_score, oi_adj, sent_adj, adjusted_score, adjusted_level
            )

            # =========================
            # STEP 4A: SIDEWAYS CHECK -- DEDUP
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
            # LOW confidence -> block trade, send alert, skip cycle.
            # MEDIUM and HIGH -> proceed to risk check.
            # =========================

            if level == CONFIDENCE_LOW:
                logger.warning(
                    "[CONFIDENCE] Trade blocked -- LOW confidence: " +
                    str(score) + "/100 | align=" + align_sum
                )
                send_telegram(build_low_confidence_msg(timestamp, sig))
                time.sleep(SCAN_INTERVAL_SECONDS)
                continue

            logger.info(
                "[CONFIDENCE] " + level + " confidence (" + str(score) +
                "/100) -- proceeding to risk check"
            )

            # =========================
            # STEP 4C: RISK GATE
            # =========================

            risk.reload()
            risk_check = risk.check_trade_allowed(
                signal_direction=primary_direction,
                adjusted_score=score,
            )

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
            msg = build_signal_msg(timestamp, sig, trade_id, sizing, risk_summary,
                                   oi_result=oi_result, sent_result=sent_result)
            print(msg)
            send_telegram(msg)
            logger.info("[TELEGRAM] Signal sent: " + trade_id)

            # --- Excel entry row (includes confidence, MTF, OI, sentiment columns) ---
            trade_row = {
                "Date":              today,
                "Time":              timestamp,
                "Trend":             sig.get("trend", primary_direction),
                "Current Price":     entry_price,
                "VWAP":              vwap_entry,
                "EMA9":              ema9_entry,
                "Trade Signal":      sig["trade_signal"],
                "Stop Loss":         sig.get("stop_loss", ""),
                "Target 1":          sig.get("target1", ""),
                "Target 2":          sig.get("target2", ""),
                "Target 3":          sig.get("target3", ""),
                "Lots":              sizing["lots"],
                "Max Loss INR":      sizing["max_loss_inr"],
                "MTF Alignment":     align_sum,
                "Base Score":        base_score,
                "OI Adjustment":     oi_adj,
                "Sentiment Adj":     sent_adj,
                "Confidence Score":  score,
                "Confidence Level":  level,
                "TF 5m":             tf_data.get("5m", {}).get("direction", "N/A"),
                "TF 15m":            tf_data.get("15m", {}).get("direction", "N/A"),
                "TF 1h":             tf_data.get("1h", {}).get("direction", "N/A"),
                "PCR":               (oi_result or {}).get("pcr"),
                "Max Pain":          (oi_result or {}).get("max_pain"),
                "ATM OI Bias":       (oi_result or {}).get("atm_bias", "N/A"),
                "India VIX":         (sent_result or {}).get("vix"),
                "US Futures Pct":    (sent_result or {}).get("us_futures_pct"),
                "News Sentiment":    (sent_result or {}).get("sentiment", "N/A"),
                "Trade ID":          trade_id,
                "Trade Status":      TradeStateManager.OPEN,
                "Outcome":           None,
                "Points Result":     None,
                "Exit Price":        None,
                "Exit Time":         None,
            }
            append_trade_log(trade_row)
            logger.info("[LOG] Entry row written: trade_id=" + trade_id)

            logger.info("[LOOP] Done. Sleeping " + str(SCAN_INTERVAL_SECONDS) + "s...")
            time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
