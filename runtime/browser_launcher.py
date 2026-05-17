"""
runtime/browser_launcher.py
============================
Chrome browser process launcher and health monitor for TradingView and Groww.

Each browser runs as an independent OS process with a dedicated Chrome profile
and remote debugging port. This module:

  - Auto-kills any process already holding the target port (no manual cleanup needed)
  - Validates Chrome profile directory exists before launch
  - Launches Chrome via subprocess.Popen
  - Polls the CDP /json endpoint until the browser is ready
  - Exposes health checks (is the browser still responding?)
  - Relaunches a dead browser and re-waits for CDP readiness

IMPORTANT: This module never touches Playwright. It only manages the Chrome
OS process. Playwright modules connect to the CDP port independently.
"""

import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import requests

from config.config import (
    CHROME_EXE_PATH,
    TRADINGVIEW_CDP_PORT,
    TRADINGVIEW_PROFILE_DIR,
    TRADINGVIEW_URL,
    GROWW_CDP_PORT,
    GROWW_PROFILE_DIR,
    GROWW_FNO_URL,
    BROWSER_READY_TIMEOUT_S,
    BROWSER_READY_POLL_INTERVAL_S,
)

logger = logging.getLogger(__name__)

# =========================
# CDP ENDPOINT HELPERS
# =========================

def _cdp_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/json"


def wait_cdp_ready(port: int, timeout_s: int = BROWSER_READY_TIMEOUT_S) -> bool:
    """
    Poll the Chrome DevTools /json endpoint until it responds or timeout expires.
    Returns True if browser is ready, False on timeout.
    """
    url = _cdp_url(port)
    deadline = time.monotonic() + timeout_s
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        try:
            resp = requests.get(url, timeout=3)
            if resp.status_code == 200:
                tabs = resp.json()
                logger.info(
                    f"[BROWSER] CDP port {port} ready — "
                    f"{len(tabs)} tab(s) detected (attempt {attempt})"
                )
                return True
        except Exception:
            pass
        logger.debug(f"[BROWSER] Port {port} not ready yet (attempt {attempt}) — retrying...")
        time.sleep(BROWSER_READY_POLL_INTERVAL_S)
    logger.error(f"[BROWSER] CDP port {port} did not become ready within {timeout_s}s")
    return False


def browser_is_alive(port: int) -> bool:
    """
    Quick health check: return True if the CDP endpoint responds.
    Used by the watchdog loop every WATCHDOG_POLL_INTERVAL_S seconds.
    """
    try:
        resp = requests.get(_cdp_url(port), timeout=3)
        return resp.status_code == 200
    except Exception:
        return False


def get_cdp_tabs(port: int) -> list:
    """Return list of open tab descriptors from CDP, or [] on failure."""
    try:
        resp = requests.get(_cdp_url(port), timeout=3)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return []


# =========================
# PORT CONFLICT RESOLUTION
# =========================

def _kill_process_on_port(port: int) -> bool:
    """
    Detect and kill any process occupying a TCP port on Windows.
    Uses netstat + taskkill so the user never has to do this manually.
    Safe no-op on Linux/Mac (returns False).
    Returns True if a conflicting process was found and killed.
    """
    if sys.platform != "win32":
        return False
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, timeout=10,
        )
        pid = None
        for line in result.stdout.splitlines():
            # Match lines like: TCP  0.0.0.0:9222  0.0.0.0:0  LISTENING  1234
            if f":{port}" in line and "LISTENING" in line:
                parts = line.split()
                if parts:
                    try:
                        pid = int(parts[-1])
                    except ValueError:
                        pass
                break

        if pid is None or pid == 0:
            return False  # Port is free

        logger.warning(
            f"[BROWSER] Port {port} already occupied by PID {pid} — "
            f"killing automatically before Chrome launch."
        )
        kill_result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True, text=True, timeout=10,
        )
        if kill_result.returncode == 0:
            logger.info(f"[BROWSER] Killed PID {pid} on port {port} — port now free.")
            time.sleep(1)  # Let OS release the port
            return True
        else:
            logger.warning(
                f"[BROWSER] taskkill failed for PID {pid}: {kill_result.stderr.strip()}"
            )
            return False

    except Exception as exc:
        logger.warning(f"[BROWSER] Port {port} conflict check error: {exc}")
        return False


# =========================
# PROFILE DIRECTORY CHECK
# =========================

def _ensure_profile_dir(profile_dir: str, label: str) -> bool:
    """
    Verify the Chrome profile directory exists.
    If missing, logs a clear error with remediation hint.
    Returns True if exists, False otherwise.
    """
    path = Path(profile_dir)
    if path.exists():
        return True
    logger.error(
        f"[BROWSER] {label} Chrome profile NOT FOUND: {profile_dir}\n"
        f"  Fix: Open Chrome manually, sign in, close it — "
        f"then set the correct path in .env as GROWW_PROFILE_DIR or TRADINGVIEW_PROFILE_DIR."
    )
    return False


# =========================
# CHROME PROCESS LAUNCHER
# =========================

def _build_chrome_args(profile_dir: str, port: int, url: str) -> list:
    """Build the chrome.exe argument list."""
    return [
        CHROME_EXE_PATH,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-client-side-phishing-detection",
        "--disable-default-apps",
        "--disable-hang-monitor",
        "--disable-popup-blocking",
        "--disable-prompt-on-repost",
        "--disable-sync",
        "--disable-translate",
        "--metrics-recording-only",
        "--safebrowsing-disable-auto-update",
        url,
    ]


def _launch_chrome(profile_dir: str, port: int, url: str, label: str) -> Optional[subprocess.Popen]:
    """
    Launch a Chrome process and return the Popen handle.
    Auto-kills any existing process on the target port before launching.
    Validates the profile directory exists before attempting launch.
    Returns None if launch fails.
    """
    # --- Auto-kill any process already holding this port ---
    _kill_process_on_port(port)

    # --- Verify profile directory exists ---
    if not _ensure_profile_dir(profile_dir, label):
        return None

    args = _build_chrome_args(profile_dir, port, url)
    try:
        # CREATE_NEW_PROCESS_GROUP keeps Chrome alive when parent exits on Windows
        creation_flags = 0
        if sys.platform == "win32":
            creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP

        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
        logger.info(f"[BROWSER] Launched {label} Chrome — PID {proc.pid}, port {port}")
        return proc
    except FileNotFoundError:
        logger.critical(
            f"[BROWSER] Chrome executable not found: {CHROME_EXE_PATH}\n"
            "Set CHROME_EXE_PATH in .env or verify Chrome installation."
        )
        return None
    except Exception as exc:
        logger.error(f"[BROWSER] Failed to launch {label} Chrome: {exc}")
        return None


# =========================
# PUBLIC LAUNCHERS
# =========================

def launch_tradingview_browser() -> Optional[subprocess.Popen]:
    """
    Launch the TradingView Chrome profile on port 9222.
    Opens the NIFTY chart URL so the bookmarked chart loads immediately.
    """
    logger.info("[BROWSER] Launching TradingView Chrome profile...")
    return _launch_chrome(
        profile_dir=TRADINGVIEW_PROFILE_DIR,
        port=TRADINGVIEW_CDP_PORT,
        url=TRADINGVIEW_URL,
        label="TradingView",
    )


def launch_groww_browser() -> Optional[subprocess.Popen]:
    """
    Launch the Groww Chrome profile on port 9333.
    Opens the F&O page URL on startup.
    """
    logger.info("[BROWSER] Launching Groww Chrome profile...")
    return _launch_chrome(
        profile_dir=GROWW_PROFILE_DIR,
        port=GROWW_CDP_PORT,
        url=GROWW_FNO_URL,
        label="Groww",
    )


# =========================
# RECONNECT / RELAUNCH
# =========================

def relaunch_tradingview_browser(
    old_proc: Optional[subprocess.Popen],
) -> Optional[subprocess.Popen]:
    """
    Terminate the old TradingView Chrome process (if any) and relaunch.
    Waits for CDP readiness before returning.
    Returns new Popen on success, None on failure.
    """
    _terminate_safely(old_proc, "TradingView")
    proc = launch_tradingview_browser()
    if proc is None:
        return None
    if not wait_cdp_ready(TRADINGVIEW_CDP_PORT):
        logger.error("[BROWSER] TradingView relaunch failed — CDP not ready")
        return None
    logger.info("[BROWSER] TradingView browser reconnected successfully.")
    return proc


def relaunch_groww_browser(
    old_proc: Optional[subprocess.Popen],
) -> Optional[subprocess.Popen]:
    """
    Terminate the old Groww Chrome process (if any) and relaunch.
    Waits for CDP readiness before returning.
    Returns new Popen on success, None on failure.
    """
    _terminate_safely(old_proc, "Groww")
    proc = launch_groww_browser()
    if proc is None:
        return None
    if not wait_cdp_ready(GROWW_CDP_PORT):
        logger.error("[BROWSER] Groww relaunch failed — CDP not ready")
        return None
    logger.info("[BROWSER] Groww browser reconnected successfully.")
    return proc


# =========================
# HELPERS
# =========================

def _terminate_safely(proc: Optional[subprocess.Popen], label: str) -> None:
    """Attempt graceful termination of a browser process."""
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            logger.info(f"[BROWSER] {label} Chrome process terminated (PID {proc.pid})")
    except Exception as exc:
        logger.warning(f"[BROWSER] Error terminating {label} Chrome: {exc}")
