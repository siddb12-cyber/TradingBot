"""
analytics/paper_trading_report.py
===================================
End-of-Day Paper Trading Validation Report.

Responsibilities:
    1. Generate daily + cumulative validation metric summaries
    2. Compute and display readiness score for live deployment
    3. Send EOD Telegram summary message
    4. Write formatted dashboard log to PAPER_DASHBOARD_LOG
    5. Print human-readable performance report to console

Can be run standalone at any time, or called from live_trade_tracker.py
at the EOD close event (15:30 IST).

Usage:
    # Standalone (run after market close):
    python analytics/paper_trading_report.py

    # From live_trade_tracker.py at EOD:
    from analytics.paper_trading_report import run_eod_report
    run_eod_report()
"""

import logging
import sys
from datetime import datetime
from pathlib import Path

import requests

from config.config import (
    configure_logging,
    BOT_TOKEN,
    CHAT_ID,
    PAPER_DASHBOARD_LOG,
    PAPER_TRADING_VALIDATION_END,
    PAPER_TRADING_MODE,
    READINESS_MIN_TRADES,
    READINESS_MIN_SIGNAL_ACCURACY,
    READINESS_MAX_SL_RATIO,
    READINESS_MIN_AVG_CONFIDENCE,
    READINESS_MAX_CONSEC_LOSSES,
)
from core.paper_trading_guard import get_daily_session_id, paper_tag
from core.validation_metrics import ValidationMetrics

# =========================
# LOGGING
# =========================

configure_logging()
logger = logging.getLogger(__name__)

SEP  = "=" * 52
SEP2 = "-" * 52


# =========================
# TELEGRAM
# =========================

def _send_telegram(message: str) -> bool:
    """Send message to configured CHAT_ID. Returns True on success."""
    if not BOT_TOKEN or not CHAT_ID:
        logger.warning("[REPORT] BOT_TOKEN/CHAT_ID not configured — Telegram skipped")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": message},
            timeout=10,
        )
        ok = r.status_code == 200
        if not ok:
            logger.warning(f"[REPORT] Telegram send failed: {r.status_code}")
        return ok
    except requests.RequestException as e:
        logger.error(f"[REPORT] Telegram error: {e}")
        return False


# =========================
# MESSAGE BUILDERS
# =========================

def _build_eod_telegram_msg(daily: dict, cumulative: dict) -> str:
    """
    Build the EOD Telegram summary message.
    Includes daily metrics, cumulative readiness score, and go/no-go status.
    """
    today         = datetime.now().strftime("%Y-%m-%d")
    session_id    = get_daily_session_id()
    d             = daily
    c             = cumulative
    readiness     = c.get("readiness_score", 0)
    status        = c.get("readiness_status", "NOT READY")

    # Readiness status emoji approximation
    status_label = {
        "READY":       "GO",
        "CONDITIONAL": "CONDITIONAL",
        "NOT READY":   "NOT READY",
    }.get(status, status)

    thr = d.get("target_hit_ratio", {})

    return (
        f"EOD PAPER TRADE REPORT\n{SEP2}\n"
        f"Date       : {today}\n"
        f"Session    : {session_id}\n"
        f"{SEP2}\n"
        f"TODAY\n"
        f"  Trades   : {d.get('total_trades', 0)}\n"
        f"  Accuracy : {d.get('signal_accuracy_pct', 0.0)}%\n"
        f"  SL Ratio : {d.get('sl_ratio_pct', 0.0)}%\n"
        f"  Avg Conf : {d.get('avg_confidence_score', 0.0)}/100\n"
        f"  T1/T2/T3 : {thr.get('t1_pct',0)*100:.0f}% / {thr.get('t2_pct',0)*100:.0f}% / {thr.get('t3_pct',0)*100:.0f}%\n"
        f"  Rejections: {d.get('total_rejections', 0)} "
        f"(conf={d.get('rejection_confidence_low',0)} risk={d.get('rejection_risk_gate',0)})\n"
        f"  Avg Hold : {d.get('avg_holding_time_min', 0.0):.1f} min\n"
        f"{SEP2}\n"
        f"CUMULATIVE (All Days)\n"
        f"  Trades   : {c.get('total_trades', 0)} / {READINESS_MIN_TRADES} required\n"
        f"  Accuracy : {c.get('signal_accuracy_pct', 0.0)}% (need >={READINESS_MIN_SIGNAL_ACCURACY*100:.0f}%)\n"
        f"  SL Ratio : {c.get('sl_ratio_pct', 0.0)}% (need <={READINESS_MAX_SL_RATIO*100:.0f}%)\n"
        f"  Avg Conf : {c.get('avg_confidence_score', 0.0)}/100 (need >={READINESS_MIN_AVG_CONFIDENCE:.0f})\n"
        f"  Max Consec Loss: {c.get('max_consecutive_losses', 0)} (need <{READINESS_MAX_CONSEC_LOSSES})\n"
        f"  Cooldowns: {c.get('cooldown_frequency', 0)}\n"
        f"{SEP2}\n"
        f"READINESS SCORE : {readiness}/100\n"
        f"STATUS          : {status_label}\n"
        f"Validation ends : {PAPER_TRADING_VALIDATION_END}\n"
        f"{SEP2}\n"
        f"{paper_tag()}"
    )


def _build_dashboard_log_entry(daily: dict, cumulative: dict) -> str:
    """
    Build a formatted dashboard log entry for PAPER_DASHBOARD_LOG.
    Human-readable, append-only, parseable by log viewers.
    """
    now       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today     = datetime.now().strftime("%Y-%m-%d")
    session   = get_daily_session_id()
    d         = daily
    c         = cumulative
    thr       = d.get("target_hit_ratio", {})
    readiness = c.get("readiness_score", 0)
    status    = c.get("readiness_status", "NOT READY")

    lines = [
        "",
        SEP,
        f"PAPER TRADING DASHBOARD — {now}",
        f"Session ID  : {session}",
        SEP,
        f"TODAY ({today})",
        f"  Total Trades       : {d.get('total_trades', 0)}",
        f"  Signal Accuracy    : {d.get('signal_accuracy_pct', 0.0):.1f}%",
        f"  SL Hit Ratio       : {d.get('sl_ratio_pct', 0.0):.1f}%",
        f"  Avg Confidence     : {d.get('avg_confidence_score', 0.0):.1f}/100",
        f"  T1 Hit %           : {thr.get('t1_pct', 0)*100:.1f}%",
        f"  T2 Hit %           : {thr.get('t2_pct', 0)*100:.1f}%",
        f"  T3 Hit %           : {thr.get('t3_pct', 0)*100:.1f}%",
        f"  Avg Holding Time   : {d.get('avg_holding_time_min', 0.0):.1f} min",
        f"  Max Consec Losses  : {d.get('max_consecutive_losses', 0)}",
        f"  Cooldown Events    : {d.get('cooldown_frequency', 0)}",
        f"  Rejection Conf LOW : {d.get('rejection_confidence_low', 0)}",
        f"  Rejection Risk Gate: {d.get('rejection_risk_gate', 0)}",
        f"  Total Rejections   : {d.get('total_rejections', 0)}",
        SEP2,
        f"CUMULATIVE (All Validation Days)",
        f"  Total Trades       : {c.get('total_trades', 0)} / {READINESS_MIN_TRADES} required",
        f"  Signal Accuracy    : {c.get('signal_accuracy_pct', 0.0):.1f}% (>={READINESS_MIN_SIGNAL_ACCURACY*100:.0f}% needed)",
        f"  SL Hit Ratio       : {c.get('sl_ratio_pct', 0.0):.1f}% (<={READINESS_MAX_SL_RATIO*100:.0f}% needed)",
        f"  Avg Confidence     : {c.get('avg_confidence_score', 0.0):.1f}/100 (>={READINESS_MIN_AVG_CONFIDENCE:.0f} needed)",
        f"  Max Consec Losses  : {c.get('max_consecutive_losses', 0)} (<{READINESS_MAX_CONSEC_LOSSES} needed)",
        f"  Cooldown Frequency : {c.get('cooldown_frequency', 0)}",
        f"  Total Rejections   : {c.get('total_rejections', 0)}",
        SEP2,
        f"  READINESS SCORE    : {readiness}/100",
        f"  DEPLOYMENT STATUS  : {status}",
        f"  Validation Ends    : {PAPER_TRADING_VALIDATION_END}",
        SEP,
        "",
    ]
    return "\n".join(lines)


# =========================
# CONSOLE REPORT
# =========================

def _print_console_report(daily: dict, cumulative: dict) -> None:
    """Print formatted summary to stdout."""
    thr = daily.get("target_hit_ratio", {})
    print()
    print(SEP)
    print(f"  PAPER TRADING EOD REPORT — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Session : {get_daily_session_id()}")
    print(SEP)
    print(f"  TODAY")
    print(f"    Trades         : {daily.get('total_trades', 0)}")
    print(f"    Signal Accuracy: {daily.get('signal_accuracy_pct', 0.0):.1f}%")
    print(f"    SL Ratio       : {daily.get('sl_ratio_pct', 0.0):.1f}%")
    print(f"    Avg Confidence : {daily.get('avg_confidence_score', 0.0):.1f}/100")
    print(f"    T1/T2/T3       : {thr.get('t1_pct',0)*100:.0f}% / {thr.get('t2_pct',0)*100:.0f}% / {thr.get('t3_pct',0)*100:.0f}%")
    print(f"    Avg Hold Time  : {daily.get('avg_holding_time_min', 0.0):.1f} min")
    print(f"    Rejections     : {daily.get('total_rejections', 0)}")
    print(SEP2)
    print(f"  CUMULATIVE")
    print(f"    Trades         : {cumulative.get('total_trades', 0)} / {READINESS_MIN_TRADES}")
    print(f"    Signal Accuracy: {cumulative.get('signal_accuracy_pct', 0.0):.1f}%")
    print(f"    SL Ratio       : {cumulative.get('sl_ratio_pct', 0.0):.1f}%")
    print(f"    Max Consec Loss: {cumulative.get('max_consecutive_losses', 0)}")
    print(SEP2)
    rs = cumulative.get("readiness_score", 0)
    st = cumulative.get("readiness_status", "NOT READY")
    print(f"  READINESS SCORE  : {rs}/100")
    print(f"  DEPLOYMENT STATUS: {st}")
    print(f"  Validation ends  : {PAPER_TRADING_VALIDATION_END}")
    print(SEP)
    print()


# =========================
# DASHBOARD LOG WRITER
# =========================

def _write_dashboard_log(entry: str) -> None:
    """Append dashboard log entry to PAPER_DASHBOARD_LOG."""
    try:
        PAPER_DASHBOARD_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(PAPER_DASHBOARD_LOG, "a", encoding="utf-8") as f:
            f.write(entry)
        logger.info(f"[REPORT] Dashboard log updated: {PAPER_DASHBOARD_LOG.name}")
    except IOError as e:
        logger.error(f"[REPORT] Failed to write dashboard log: {e}")


# =========================
# PRIMARY PUBLIC FUNCTION
# =========================

def run_eod_report(send_telegram: bool = True) -> dict:
    """
    Run the full EOD paper trading validation report.

    Steps:
        1. Compute today's metrics
        2. Compute cumulative metrics
        3. Print console report
        4. Write dashboard log
        5. Send Telegram EOD summary (if send_telegram=True)

    Args:
        send_telegram: Whether to send the Telegram message (default True)

    Returns:
        dict with {"daily": ..., "cumulative": ..., "readiness_score": ..., "readiness_status": ...}
    """
    logger.info(SEP)
    logger.info("[REPORT] Running EOD paper trading validation report")
    logger.info(f"[REPORT] PAPER_TRADING_MODE={PAPER_TRADING_MODE}")
    logger.info(SEP)

    vm = ValidationMetrics()

    # Compute metrics
    daily      = vm.compute_daily_summary()
    cumulative = vm.compute_cumulative_summary()

    # Console output
    _print_console_report(daily, cumulative)

    # Dashboard log
    log_entry = _build_dashboard_log_entry(daily, cumulative)
    _write_dashboard_log(log_entry)

    # Telegram
    if send_telegram:
        msg = _build_eod_telegram_msg(daily, cumulative)
        sent = _send_telegram(msg)
        if sent:
            logger.info("[REPORT] EOD Telegram report sent successfully")
        else:
            logger.warning("[REPORT] Telegram send failed — report saved to log only")

    logger.info(
        f"[REPORT] Complete | readiness={cumulative.get('readiness_score')}/100 "
        f"[{cumulative.get('readiness_status')}]"
    )

    return {
        "daily":            daily,
        "cumulative":       cumulative,
        "readiness_score":  cumulative.get("readiness_score", 0),
        "readiness_status": cumulative.get("readiness_status", "NOT READY"),
    }


# =========================
# ENTRY POINT
# =========================

if __name__ == "__main__":
    result = run_eod_report(send_telegram=True)
    sys.exit(0)
