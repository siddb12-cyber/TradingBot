"""
runtime/heartbeat.py
====================
Heartbeat write / read / staleness utilities.

The runtime_manager writes heartbeats on behalf of supervised modules via
the watchdog loop (external heartbeat). Modules may also write their own
heartbeats by importing write_heartbeat() directly.

Heartbeat file format: plain UTC ISO-8601 timestamp per file.
  runtime/heartbeats/{module_name}.hb
"""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config.config import HEARTBEAT_DIR, HEARTBEAT_STALE_S

logger = logging.getLogger(__name__)

# =========================
# PATHS
# =========================

def _hb_path(module_name: str) -> Path:
    """Return the heartbeat file path for a module."""
    safe = module_name.replace(".", "_").replace("/", "_")
    return HEARTBEAT_DIR / f"{safe}.hb"


# =========================
# WRITE
# =========================

def write_heartbeat(module_name: str) -> None:
    """
    Write current UTC timestamp to the module's heartbeat file.
    Call from within a module's main loop, or from runtime_manager watchdog.
    """
    try:
        path = _hb_path(module_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()
        # Atomic write via temp file
        tmp = path.with_suffix(".tmp")
        tmp.write_text(ts, encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:
        logger.warning(f"[HEARTBEAT] Failed to write heartbeat for {module_name}: {exc}")


# =========================
# READ
# =========================

def read_heartbeat(module_name: str) -> Optional[datetime]:
    """
    Return the last heartbeat timestamp for a module, or None if absent.
    """
    path = _hb_path(module_name)
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
        return datetime.fromisoformat(raw)
    except Exception as exc:
        logger.warning(f"[HEARTBEAT] Failed to read heartbeat for {module_name}: {exc}")
        return None


# =========================
# STALENESS CHECK
# =========================

def is_stale(module_name: str, stale_after_s: int = HEARTBEAT_STALE_S) -> bool:
    """
    Return True if the heartbeat is absent or older than stale_after_s seconds.
    A missing heartbeat file is treated as stale only if the module has been
    running long enough to have written at least one beat.
    """
    last = read_heartbeat(module_name)
    if last is None:
        return False  # No heartbeat yet — module may be starting up; don't flag as stale
    now = datetime.now(timezone.utc)
    age_s = (now - last).total_seconds()
    if age_s > stale_after_s:
        logger.warning(
            f"[HEARTBEAT] {module_name} is STALE — last beat {age_s:.0f}s ago "
            f"(threshold {stale_after_s}s)"
        )
        return True
    return False


# =========================
# CLEAR
# =========================

def clear_heartbeat(module_name: str) -> None:
    """Remove the heartbeat file for a module (called on clean shutdown)."""
    path = _hb_path(module_name)
    try:
        if path.exists():
            path.unlink()
    except Exception as exc:
        logger.warning(f"[HEARTBEAT] Failed to clear heartbeat for {module_name}: {exc}")


def clear_all_heartbeats() -> None:
    """Remove all heartbeat files (called on system shutdown)."""
    try:
        for hb in HEARTBEAT_DIR.glob("*.hb"):
            hb.unlink(missing_ok=True)
        logger.info("[HEARTBEAT] All heartbeat files cleared.")
    except Exception as exc:
        logger.warning(f"[HEARTBEAT] clear_all_heartbeats error: {exc}")
