"""
app/notifier.py
===============
Windows toast notifications for TradingBot events.

Wraps winotify — falls back to logging if winotify not installed.

Requires: winotify  (pip install winotify)
"""

import logging

logger = logging.getLogger(__name__)

_WINOTIFY_AVAILABLE = False
try:
    from winotify import Notification, audio
    _WINOTIFY_AVAILABLE = True
except ImportError:
    pass

_APP_ID = "TradingBot"


# =============================================================================
# PUBLIC API
# =============================================================================

def notify(title: str, message: str, sound: bool = False) -> None:
    """
    Show a Windows toast notification.

    Falls back to a log message if winotify is unavailable.

    Parameters
    ----------
    title   : notification title
    message : body text (keep under ~120 chars for best display)
    sound   : play default notification sound
    """
    if not _WINOTIFY_AVAILABLE:
        logger.info("[Notifier] %s | %s", title, message)
        return

    try:
        toast = Notification(
            app_id=_APP_ID,
            title=title,
            msg=message,
            duration="short",
        )
        if sound:
            toast.set_audio(audio.Default, loop=False)
        toast.show()
    except Exception as exc:
        logger.warning("[Notifier] Toast failed: %s", exc)


# =============================================================================
# CONVENIENCE HELPERS
# =============================================================================

def notify_signal(direction: str, signal_text: str, score: int) -> None:
    notify(
        title=f"Signal: {direction}",
        message=f"{signal_text}  |  Score: {score}/100 — check Telegram to approve",
        sound=True,
    )


def notify_trade_open(signal_text: str, entry: float, sl: float) -> None:
    notify(
        title="Trade OPENED",
        message=f"{signal_text}  Entry: {entry:.2f}  SL: {sl:.2f}",
        sound=False,
    )


def notify_target_hit(n: int, price: float, inr: float) -> None:
    notify(
        title=f"T{n} TARGET HIT",
        message=f"Price: {price:.2f}  |  Booked: ₹{inr:,.0f}",
        sound=True,
    )


def notify_trade_closed(reason: str, pnl_pts: float, pnl_inr: float) -> None:
    icon = "PROFIT" if pnl_pts > 0 else ("LOSS" if pnl_pts < 0 else "B/E")
    notify(
        title=f"Trade CLOSED — {icon}",
        message=f"{reason}  |  {pnl_pts:+.1f}pts  ₹{pnl_inr:+,.0f}",
        sound=True,
    )
