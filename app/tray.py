"""
app/tray.py
===========
Windows system tray icon for TradingBot.

Features
--------
- Colored icon reflects current bot state in real time
  idle=gray | pending=amber | open=green | error=red
- Right-click menu:
    Open Dashboard  — opens http://localhost:5050 in default browser
    View Logs       — opens logs/trading.log in Notepad
    ─────────────
    Stop Bot        — cleanly stops the entire process

Runs in a background daemon thread; dies automatically when main process exits.

Requires: pystray  (pip install pystray)
          Pillow   (pip install Pillow)
"""

import json
import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Lazy import — graceful fallback if pystray not installed
_PYSTRAY_AVAILABLE = False
try:
    import pystray
    _PYSTRAY_AVAILABLE = True
except ImportError:
    pass

# =============================================================================
# CONSTANTS
# =============================================================================

_POLL_INTERVAL  = 10      # seconds between state polls
_DASHBOARD_URL  = "http://localhost:5050"
_BASE_DIR       = Path(__file__).parent.parent.resolve()
_STATE_FILE     = _BASE_DIR / "data" / "trade_state.json"
_LOG_FILE       = _BASE_DIR / "logs" / "trading.log"

# =============================================================================
# STATE DETECTION
# =============================================================================

def _get_bot_state() -> str:
    """
    Read data/trade_state.json and return one of:
    "idle" | "pending" | "open" | "error"
    """
    try:
        if not _STATE_FILE.exists():
            return "idle"
        data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        status = data.get("status", "IDLE")
        if status == "IDLE":
            return "idle"
        elif status == "PENDING":
            return "pending"
        elif status == "OPEN":
            return "open"
        else:
            return "idle"
    except Exception:
        return "error"


def _get_status_label() -> str:
    """Short human-readable status for tray tooltip."""
    try:
        if not _STATE_FILE.exists():
            return "TradingBot — Idle"
        data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        status = data.get("status", "IDLE")
        if status == "OPEN":
            pnl = data.get("pnl_points", 0)
            sig = data.get("signal_text", "")
            return f"TradingBot — OPEN {sig} P&L={pnl:+.1f}pts"
        elif status == "PENDING":
            return "TradingBot — PENDING approval"
        return "TradingBot — Idle"
    except Exception:
        return "TradingBot"


# =============================================================================
# MENU ACTIONS
# =============================================================================

def _open_dashboard(_icon=None, _item=None) -> None:
    """Open the live dashboard in the default browser."""
    webbrowser.open(_DASHBOARD_URL)
    logger.info("[Tray] Dashboard opened: %s", _DASHBOARD_URL)


def _view_logs(_icon=None, _item=None) -> None:
    """Open trading.log in Notepad."""
    try:
        if _LOG_FILE.exists():
            os.startfile(str(_LOG_FILE))
        else:
            webbrowser.open(_DASHBOARD_URL)
    except Exception as exc:
        logger.warning("[Tray] Could not open log file: %s", exc)


def _stop_bot(icon, _item=None) -> None:
    """Stop the entire bot process cleanly."""
    logger.info("[Tray] Stop requested via tray menu")
    try:
        icon.stop()
    except Exception:
        pass
    # Give pystray a moment to clean up, then exit
    threading.Timer(0.5, lambda: os.kill(os.getpid(), 9)).start()


# =============================================================================
# TRAY ICON MANAGER
# =============================================================================

class TrayApp:
    """
    Manages the pystray system tray icon.

    Usage (from main.py)
    --------------------
        tray = TrayApp()
        tray.start()          # starts in a daemon thread, non-blocking
        tray.notify_signal()  # call from engine to push notifications
    """

    def __init__(self) -> None:
        self._icon: Optional[object]  = None
        self._current_state: str      = "idle"
        self._thread: Optional[threading.Thread] = None

    # =========================================================================
    # STARTUP
    # =========================================================================

    def start(self) -> None:
        """Launch tray icon in a daemon thread. No-op if pystray unavailable."""
        if not _PYSTRAY_AVAILABLE:
            logger.warning(
                "[Tray] pystray not installed — tray icon disabled. "
                "Run: pip install pystray Pillow"
            )
            return

        self._thread = threading.Thread(
            target=self._run, name="TrayIcon", daemon=True
        )
        self._thread.start()
        logger.info("[Tray] System tray started")

    # =========================================================================
    # NOTIFICATION HELPERS (called by engine/notifier)
    # =========================================================================

    def update_state(self, state: str) -> None:
        """Force an immediate icon color update. state: idle/pending/open/error"""
        self._current_state = state
        self._refresh_icon()

    # =========================================================================
    # INTERNAL
    # =========================================================================

    def _run(self) -> None:
        """Main tray loop — runs inside daemon thread."""
        from app.icons import make_icon

        initial_img = make_icon("idle")
        if initial_img is None:
            logger.warning("[Tray] Pillow not installed — icon will be blank")

        menu = pystray.Menu(
            pystray.MenuItem("Open Dashboard", _open_dashboard, default=True),
            pystray.MenuItem("View Logs",      _view_logs),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Stop Bot",       _stop_bot),
        )

        self._icon = pystray.Icon(
            name    = "TradingBot",
            icon    = initial_img,
            title   = "TradingBot",
            menu    = menu,
        )

        # Start a background thread that polls state and updates icon color
        poller = threading.Thread(target=self._poll_state, daemon=True)
        poller.start()

        try:
            self._icon.run()
        except Exception as exc:
            logger.error("[Tray] Icon run failed: %s", exc)

    def _poll_state(self) -> None:
        """Poll state file every _POLL_INTERVAL seconds and update icon."""
        while True:
            try:
                new_state = _get_bot_state()
                if new_state != self._current_state:
                    self._current_state = new_state
                    self._refresh_icon()
                    logger.debug("[Tray] State changed to: %s", new_state)
                # Update tooltip with live P&L
                if self._icon:
                    self._icon.title = _get_status_label()
            except Exception as exc:
                logger.debug("[Tray] Poll error: %s", exc)
            time.sleep(_POLL_INTERVAL)

    def _refresh_icon(self) -> None:
        """Redraw tray icon with color matching current state."""
        if not self._icon:
            return
        try:
            from app.icons import make_icon
            new_img = make_icon(self._current_state)
            if new_img:
                self._icon.icon = new_img
        except Exception as exc:
            logger.debug("[Tray] Icon refresh error: %s", exc)


# =============================================================================
# MODULE-LEVEL SINGLETON
# =============================================================================

_tray_instance: Optional[TrayApp] = None


def start() -> TrayApp:
    """
    Create and start the global TrayApp singleton.
    Safe to call multiple times — only creates one instance.
    """
    global _tray_instance
    if _tray_instance is None:
        _tray_instance = TrayApp()
        _tray_instance.start()
    return _tray_instance


def get() -> Optional[TrayApp]:
    """Return the running TrayApp instance, or None if not started."""
    return _tray_instance
