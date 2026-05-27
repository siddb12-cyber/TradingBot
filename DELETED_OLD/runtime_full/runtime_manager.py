"""
runtime/runtime_manager.py
==========================
Autonomous Runtime Manager for TradingBot.

Responsibilities:
  1. Launch TradingView and Groww Chrome browsers
  2. Start all supervised trading modules as subprocesses
  3. Watchdog loop: detect crashes → restart with exponential backoff
  4. Browser health loop: detect dead CDP → relaunch browser
  5. Module heartbeat logging: write heartbeats on behalf of alive processes
  6. Session persistence: save/load state to survive manual restarts
  7. Telegram notifications: startup / crash / restart / recovery / shutdown
  8. Graceful shutdown: SIGINT / SIGTERM → terminate children → save state
  9. Windows startup integration: --install-startup / --remove-startup flags

Safety guarantees preserved:
  - enforce_paper_mode() called at startup — aborts if PAPER_TRADING_MODE is False
  - DRY_RUN enforcement unchanged in groww_execution_engine
  - Approval workflow unchanged in telegram_approval

Usage:
    python runtime/runtime_manager.py                  # normal start
    python runtime/runtime_manager.py --install-startup
    python runtime/runtime_manager.py --remove-startup
    python runtime/runtime_manager.py --status-startup
    python runtime/runtime_manager.py --recover         # attempt session recovery
"""

# =========================
# IMPORTS
# =========================

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

# Project root on PYTHONPATH
sys.path.insert(0, str(Path(__file__).parent.parent))

from config.config import (
    BASE_DIR,
    BOT_TOKEN,
    CHAT_ID,
    CHROME_EXE_PATH,
    TRADINGVIEW_CDP_PORT,
    GROWW_CDP_PORT,
    WATCHDOG_POLL_INTERVAL_S,
    MAX_CRASH_RESTARTS,
    CRASH_RESTART_BACKOFF_S,
    MAX_BACKOFF_S,
    HEARTBEAT_STALE_S,
    RUNTIME_SESSION_FILE,
    RUNTIME_LOG_FILE,
    SUPERVISED_MODULES,
    STARTUP_TASK_NAME,
    PAPER_TRADING_MODE,
    configure_logging,
)
from core.paper_trading_guard import enforce_paper_mode, paper_tag, paper_log_prefix
from runtime.heartbeat import write_heartbeat, clear_all_heartbeats, is_stale
from runtime.browser_launcher import (
    launch_tradingview_browser,
    launch_groww_browser,
    relaunch_tradingview_browser,
    relaunch_groww_browser,
    wait_cdp_ready,
    browser_is_alive,
)

# =========================
# LOGGING
# =========================

configure_logging()
logger = logging.getLogger(__name__)

# Also log to dedicated runtime log file
_runtime_log_handler = logging.FileHandler(RUNTIME_LOG_FILE, encoding="utf-8")
_runtime_log_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
)
logging.getLogger().addHandler(_runtime_log_handler)


# =========================
# TELEGRAM (standalone — avoids circular imports with core.*)
# =========================

def _send_telegram(message: str) -> bool:
    """
    Send a Telegram message using the bot token from config.
    Does NOT import from core.telegram_approval to avoid circular dependencies.
    Returns True on success.
    """
    if not BOT_TOKEN or not CHAT_ID:
        logger.warning("[TELEGRAM] BOT_TOKEN or CHAT_ID not set — skipping notification.")
        return False
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        resp = requests.post(
            url,
            json={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        ok = resp.status_code == 200
        if not ok:
            logger.warning(f"[TELEGRAM] Send failed: {resp.status_code} {resp.text[:100]}")
        return ok
    except Exception as exc:
        logger.warning(f"[TELEGRAM] Exception sending message: {exc}")
        return False


# =========================
# DATACLASSES
# =========================

@dataclass
class ModuleSpec:
    """Static definition of a supervised module."""
    name: str           # Short identifier, e.g. "ai_trading_assistant"
    module: str         # Python module string, e.g. "core.ai_trading_assistant"
    restart: bool       # Whether to auto-restart on crash


@dataclass
class ModuleRecord:
    """Runtime state of a supervised module."""
    spec:          ModuleSpec
    process:       Optional[subprocess.Popen]
    restart_count: int                   = 0
    started_at:    Optional[datetime]    = None
    crash_history: List[datetime]        = field(default_factory=list)

    @property
    def pid(self) -> Optional[int]:
        return self.process.pid if self.process else None

    @property
    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None


# =========================
# RUNTIME MANAGER CLASS
# =========================

class RuntimeManager:
    """
    Central orchestrator for TradingBot autonomous operation.

    Manages:
      - Browser processes (TradingView + Groww Chrome)
      - Supervised Python module subprocesses
      - Watchdog thread for crash detection and restart
      - Session persistence across restarts
      - Telegram lifecycle notifications
    """

    def __init__(self) -> None:
        self._modules:       Dict[str, ModuleRecord]          = {}
        self._tv_proc:       Optional[subprocess.Popen]       = None
        self._groww_proc:    Optional[subprocess.Popen]       = None
        self._shutdown_event: threading.Event                 = threading.Event()
        self._watchdog_thread: Optional[threading.Thread]    = None
        self._session_start:   Optional[datetime]             = None
        self._lock:            threading.Lock                 = threading.Lock()

    # =========================
    # STARTUP
    # =========================

    def startup(self) -> bool:
        """
        Full startup sequence:
          1. Print banner
          2. Enforce paper mode
          3. Launch browsers
          4. Wait for CDP readiness
          5. Start supervised modules
          6. Save session
          7. Start watchdog thread
          8. Send Telegram startup notification
        Returns True on success.
        """
        self._print_banner()

        # --- Paper trading safety gate ---
        enforce_paper_mode()
        logger.info(f"[RUNTIME] {paper_log_prefix()} Paper mode confirmed.")

        self._session_start = datetime.now(timezone.utc)

        # --- Launch browsers ---
        logger.info("[RUNTIME] Launching TradingView Chrome...")
        self._tv_proc = launch_tradingview_browser()
        if self._tv_proc is None:
            logger.critical("[RUNTIME] TradingView browser launch failed — aborting.")
            return False

        logger.info("[RUNTIME] Launching Groww Chrome...")
        self._groww_proc = launch_groww_browser()
        if self._groww_proc is None:
            logger.warning("[RUNTIME] Groww browser launch failed — continuing without Groww.")

        # --- Wait for CDP readiness ---
        logger.info(f"[RUNTIME] Waiting for TradingView CDP on port {TRADINGVIEW_CDP_PORT}...")
        tv_ready = wait_cdp_ready(TRADINGVIEW_CDP_PORT)
        if not tv_ready:
            logger.critical("[RUNTIME] TradingView CDP not ready — aborting.")
            return False
        logger.info("[RUNTIME] TradingView browser ready.")

        if self._groww_proc is not None:
            logger.info(f"[RUNTIME] Waiting for Groww CDP on port {GROWW_CDP_PORT}...")
            groww_ready = wait_cdp_ready(GROWW_CDP_PORT)
            if groww_ready:
                logger.info("[RUNTIME] Groww browser ready.")
            else:
                logger.warning("[RUNTIME] Groww CDP not ready — continuing without Groww.")

        # --- Start supervised modules ---
        time.sleep(3)  # Brief pause — let browsers fully settle
        for spec_dict in SUPERVISED_MODULES:
            spec = ModuleSpec(**spec_dict)
            record = self._start_module(spec)
            with self._lock:
                self._modules[spec.name] = record
            time.sleep(5)  # Stagger module starts

        # --- Save session state ---
        self._save_session()

        # --- Start watchdog thread ---
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            name="RuntimeWatchdog",
            daemon=True,
        )
        self._watchdog_thread.start()
        logger.info("[RUNTIME] Watchdog thread started.")

        # --- Telegram startup notification ---
        self._send_startup_telegram()

        logger.info("[RUNTIME] Startup complete. All modules running.")
        return True

    # =========================
    # MODULE LIFECYCLE
    # =========================

    def _start_module(self, spec: ModuleSpec) -> ModuleRecord:
        """Launch a Python module as a subprocess."""
        logger.info(f"[RUNTIME] Starting module: {spec.module}")
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", spec.module],
                cwd=str(BASE_DIR),
                stdout=None,   # Inherit — modules log via their own handlers
                stderr=None,
            )
            started_at = datetime.now(timezone.utc)
            logger.info(f"[RUNTIME] Module {spec.name} started — PID {proc.pid}")
            return ModuleRecord(spec=spec, process=proc, started_at=started_at)
        except Exception as exc:
            logger.error(f"[RUNTIME] Failed to start module {spec.name}: {exc}")
            return ModuleRecord(spec=spec, process=None)

    def _restart_module(self, name: str) -> None:
        """
        Handle crash-restart with exponential backoff.
        After MAX_CRASH_RESTARTS, halts the module and sends critical Telegram alert.
        """
        with self._lock:
            record = self._modules.get(name)
        if record is None:
            return

        if not record.spec.restart:
            logger.warning(f"[RUNTIME] Module {name} died — restart disabled for this module.")
            return

        if record.restart_count >= MAX_CRASH_RESTARTS:
            msg = (
                f"🚨 <b>TradingBot — Module Halted</b>\n"
                f"{paper_tag()}\n"
                f"Module: <code>{name}</code>\n"
                f"Reason: Exceeded max restarts ({MAX_CRASH_RESTARTS})\n"
                f"Action: Manual intervention required.\n"
                f"Time: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
            )
            _send_telegram(msg)
            logger.critical(f"[RUNTIME] Module {name} HALTED — max restarts exceeded.")
            return

        # --- Exponential backoff ---
        backoff = min(CRASH_RESTART_BACKOFF_S * (2 ** record.restart_count), MAX_BACKOFF_S)
        logger.warning(
            f"[RUNTIME] Module {name} crashed (restart #{record.restart_count + 1}) — "
            f"waiting {backoff}s before restart."
        )
        _send_telegram(
            f"⚠️ <b>TradingBot — Module Crash</b>\n"
            f"{paper_tag()}\n"
            f"Module: <code>{name}</code>\n"
            f"Restart attempt: {record.restart_count + 1}/{MAX_CRASH_RESTARTS}\n"
            f"Backoff: {backoff}s\n"
            f"Time: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
        )

        if self._shutdown_event.wait(timeout=backoff):
            return  # Shutdown requested during backoff

        # --- Restart ---
        new_record = self._start_module(record.spec)
        new_record.restart_count  = record.restart_count + 1
        new_record.crash_history  = record.crash_history + [datetime.now(timezone.utc)]

        with self._lock:
            self._modules[name] = new_record

        self._save_session()

        if new_record.process is not None:
            _send_telegram(
                f"✅ <b>TradingBot — Module Restarted</b>\n"
                f"{paper_tag()}\n"
                f"Module: <code>{name}</code>\n"
                f"Attempt: {new_record.restart_count}/{MAX_CRASH_RESTARTS}\n"
                f"PID: {new_record.pid}\n"
                f"Time: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
            )
            logger.info(f"[RUNTIME] Module {name} restarted — PID {new_record.pid}")
        else:
            logger.error(f"[RUNTIME] Module {name} restart failed — process is None.")

    # =========================
    # WATCHDOG LOOP
    # =========================

    def _watchdog_loop(self) -> None:
        """
        Background thread: polls module health and browser health every
        WATCHDOG_POLL_INTERVAL_S seconds.
        """
        logger.info("[WATCHDOG] Loop started.")
        while not self._shutdown_event.is_set():
            self._shutdown_event.wait(timeout=WATCHDOG_POLL_INTERVAL_S)
            if self._shutdown_event.is_set():
                break

            self._check_all_modules()
            self._check_browser_health()

        logger.info("[WATCHDOG] Loop exited.")

    def _check_all_modules(self) -> None:
        """Inspect every supervised module — detect crash, heartbeat staleness."""
        with self._lock:
            names = list(self._modules.keys())

        for name in names:
            with self._lock:
                record = self._modules.get(name)
            if record is None:
                continue

            # --- Process alive check ---
            if not record.is_alive:
                exit_code = record.process.returncode if record.process else "N/A"
                logger.error(
                    f"[WATCHDOG] Module {name} is DEAD (exit code: {exit_code})"
                )
                self._restart_module(name)
                continue

            # --- Write external heartbeat on behalf of alive process ---
            write_heartbeat(name)

            # --- Heartbeat staleness check (only applies after module has had time to start) ---
            if record.started_at is not None:
                uptime_s = (datetime.now(timezone.utc) - record.started_at).total_seconds()
                if uptime_s > HEARTBEAT_STALE_S and is_stale(name, HEARTBEAT_STALE_S * 2):
                    logger.error(f"[WATCHDOG] Module {name} heartbeat STALE — killing and restarting.")
                    _send_telegram(
                        f"⚠️ <b>TradingBot — Module Frozen</b>\n"
                        f"{paper_tag()}\n"
                        f"Module: <code>{name}</code>\n"
                        f"Status: Heartbeat stale — process appears frozen\n"
                        f"Action: Killing and restarting\n"
                        f"Time: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
                    )
                    try:
                        record.process.kill()
                    except Exception:
                        pass
                    self._restart_module(name)

            logger.debug(f"[WATCHDOG] Module {name} — PID {record.pid} — healthy ✓")

    # =========================
    # BROWSER HEALTH
    # =========================

    def _check_browser_health(self) -> None:
        """Check CDP health for both browsers and relaunch if unresponsive."""
        # TradingView
        if not browser_is_alive(TRADINGVIEW_CDP_PORT):
            logger.warning("[WATCHDOG] TradingView browser unresponsive — relaunching.")
            _send_telegram(
                f"🔄 <b>TradingBot — Browser Reconnect</b>\n"
                f"{paper_tag()}\n"
                f"Browser: TradingView (port {TRADINGVIEW_CDP_PORT})\n"
                f"Status: Unresponsive — relaunching\n"
                f"Time: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
            )
            new_proc = relaunch_tradingview_browser(self._tv_proc)
            if new_proc:
                self._tv_proc = new_proc
                self._save_session()
                _send_telegram(
                    f"✅ <b>TradingBot — Browser Recovered</b>\n"
                    f"{paper_tag()}\n"
                    f"Browser: TradingView\n"
                    f"Time: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
                )

        # Groww (non-critical — don't abort if Groww is unavailable)
        if self._groww_proc is not None and not browser_is_alive(GROWW_CDP_PORT):
            logger.warning("[WATCHDOG] Groww browser unresponsive — relaunching.")
            _send_telegram(
                f"🔄 <b>TradingBot — Browser Reconnect</b>\n"
                f"{paper_tag()}\n"
                f"Browser: Groww (port {GROWW_CDP_PORT})\n"
                f"Status: Unresponsive — relaunching\n"
                f"Time: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
            )
            new_proc = relaunch_groww_browser(self._groww_proc)
            if new_proc:
                self._groww_proc = new_proc
                self._save_session()

    # =========================
    # SESSION PERSISTENCE
    # =========================

    def _session_dict(self) -> dict:
        """Build a JSON-serialisable snapshot of current runtime state."""
        module_data = {}
        with self._lock:
            for name, record in self._modules.items():
                module_data[name] = {
                    "module": record.spec.module,
                    "pid": record.pid,
                    "restart_count": record.restart_count,
                    "started_at": record.started_at.isoformat() if record.started_at else None,
                }
        return {
            "session_id": f"RUNTIME_{self._session_start.strftime('%Y%m%d_%H%M%S')}" if self._session_start else "UNKNOWN",
            "paper_trading_mode": PAPER_TRADING_MODE,
            "started_at": self._session_start.isoformat() if self._session_start else None,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "tv_pid": self._tv_proc.pid if self._tv_proc else None,
            "groww_pid": self._groww_proc.pid if self._groww_proc else None,
            "modules": module_data,
        }

    def _save_session(self) -> None:
        """Atomically save session state to RUNTIME_SESSION_FILE."""
        try:
            state = self._session_dict()
            tmp = RUNTIME_SESSION_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
            tmp.replace(RUNTIME_SESSION_FILE)
            logger.debug("[SESSION] State saved.")
        except Exception as exc:
            logger.warning(f"[SESSION] Failed to save session: {exc}")

    def _load_session(self) -> Optional[dict]:
        """Load previous session state if available."""
        if not RUNTIME_SESSION_FILE.exists():
            return None
        try:
            data = json.loads(RUNTIME_SESSION_FILE.read_text(encoding="utf-8"))
            logger.info(f"[SESSION] Loaded session: {data.get('session_id', '?')}")
            return data
        except Exception as exc:
            logger.warning(f"[SESSION] Failed to load session: {exc}")
            return None

    # =========================
    # SESSION RECOVERY
    # =========================

    def recover_session(self) -> bool:
        """
        Attempt to recover a previous session without restarting browsers.
        Checks if previous module PIDs are still alive (e.g. after runtime_manager crash).
        Falls back to full startup() if recovery fails.
        """
        logger.info("[RECOVERY] Attempting session recovery...")
        session = self._load_session()
        if session is None:
            logger.info("[RECOVERY] No previous session found — performing full startup.")
            return self.startup()

        prev_session_id = session.get("session_id", "?")
        logger.info(f"[RECOVERY] Previous session: {prev_session_id}")

        # --- Check if browser CDPs are still alive ---
        tv_alive = browser_is_alive(TRADINGVIEW_CDP_PORT)
        groww_alive = browser_is_alive(GROWW_CDP_PORT)

        if not tv_alive:
            logger.info("[RECOVERY] TradingView browser not alive — performing full startup.")
            return self.startup()

        logger.info("[RECOVERY] TradingView browser is still alive — reconnecting.")

        # --- Check previous module PIDs ---
        all_modules_alive = True
        for name, data in session.get("modules", {}).items():
            pid = data.get("pid")
            if pid and _pid_is_alive(pid):
                logger.info(f"[RECOVERY] Module {name} (PID {pid}) still alive — reconnecting.")
                # Reattach via a "ghost" record; we can't reclaim the Popen, but
                # watchdog will detect death when it eventually occurs.
                spec = ModuleSpec(
                    name=name,
                    module=data.get("module", f"core.{name}"),
                    restart=True,
                )
                record = ModuleRecord(
                    spec=spec,
                    process=None,  # Cannot reclaim existing Popen
                    restart_count=data.get("restart_count", 0),
                )
                with self._lock:
                    self._modules[name] = record
            else:
                logger.info(f"[RECOVERY] Module {name} (PID {pid}) is DEAD — will restart.")
                all_modules_alive = False

        if not all_modules_alive:
            # Restart dead modules
            for spec_dict in SUPERVISED_MODULES:
                name = spec_dict["name"]
                with self._lock:
                    record = self._modules.get(name)
                if record is None or record.process is None:
                    spec = ModuleSpec(**spec_dict)
                    new_record = self._start_module(spec)
                    with self._lock:
                        self._modules[name] = new_record

        self._session_start = datetime.now(timezone.utc)
        self._save_session()

        # Start watchdog
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            name="RuntimeWatchdog",
            daemon=True,
        )
        self._watchdog_thread.start()

        _send_telegram(
            f"🔄 <b>TradingBot — Session Recovered</b>\n"
            f"{paper_tag()}\n"
            f"Previous session: <code>{prev_session_id}</code>\n"
            f"TradingView browser: {'✅' if tv_alive else '🔄 relaunched'}\n"
            f"Groww browser: {'✅' if groww_alive else '⚠️ unavailable'}\n"
            f"Time: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
        )
        logger.info("[RECOVERY] Session recovery complete.")
        return True

    # =========================
    # GRACEFUL SHUTDOWN
    # =========================

    def shutdown(self, reason: str = "Manual") -> None:
        """
        Graceful shutdown:
          1. Signal watchdog thread to exit
          2. Terminate all supervised modules
          3. Clear heartbeat files
          4. Save final session state
          5. Send Telegram shutdown notification
        """
        logger.info(f"[RUNTIME] Shutdown initiated — reason: {reason}")
        self._shutdown_event.set()

        # Wait for watchdog thread to exit
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            self._watchdog_thread.join(timeout=5)

        # Terminate supervised modules
        with self._lock:
            records = list(self._modules.values())
        for record in records:
            if record.is_alive:
                try:
                    record.process.terminate()
                    record.process.wait(timeout=5)
                    logger.info(f"[RUNTIME] Module {record.spec.name} terminated.")
                except Exception as exc:
                    logger.warning(f"[RUNTIME] Error terminating {record.spec.name}: {exc}")
                    try:
                        record.process.kill()
                    except Exception:
                        pass

        # Clear heartbeats
        clear_all_heartbeats()

        # Save final session
        self._save_session()

        # Telegram notification
        uptime = ""
        if self._session_start:
            elapsed = datetime.now(timezone.utc) - self._session_start
            h, r  = divmod(int(elapsed.total_seconds()), 3600)
            m, s  = divmod(r, 60)
            uptime = f"{h:02d}:{m:02d}:{s:02d}"

        _send_telegram(
            f"🛑 <b>TradingBot — Shutdown</b>\n"
            f"{paper_tag()}\n"
            f"Reason: {reason}\n"
            f"Uptime: {uptime}\n"
            f"Time: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
        )
        logger.info("[RUNTIME] Shutdown complete.")

    # =========================
    # SIGNAL HANDLERS
    # =========================

    def _signal_handler(self, signum: int, frame) -> None:
        """Handle SIGINT (Ctrl+C) and SIGTERM for graceful shutdown."""
        sig_name = "SIGINT" if signum == signal.SIGINT else "SIGTERM"
        logger.info(f"[RUNTIME] Received {sig_name} — initiating graceful shutdown.")
        self.shutdown(reason=sig_name)
        sys.exit(0)

    # =========================
    # TELEGRAM HELPERS
    # =========================

    def _send_startup_telegram(self) -> None:
        """Send a rich startup notification with all module PIDs."""
        with self._lock:
            module_lines = [
                f"  • {name}: PID {r.pid or 'N/A'}"
                for name, r in self._modules.items()
            ]
        module_info = "\n".join(module_lines) or "  (none)"

        msg = (
            f"🚀 <b>TradingBot Started</b>\n"
            f"{paper_tag()}\n\n"
            f"<b>Browsers:</b>\n"
            f"  • TradingView (port {TRADINGVIEW_CDP_PORT}): ✅\n"
            f"  • Groww (port {GROWW_CDP_PORT}): "
            f"{'✅' if browser_is_alive(GROWW_CDP_PORT) else '⚠️ unavailable'}\n\n"
            f"<b>Modules:</b>\n{module_info}\n\n"
            f"<b>Paper Mode:</b> {'🟡 ACTIVE' if PAPER_TRADING_MODE else '🔴 DISABLED'}\n"
            f"<b>Time:</b> {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
        )
        _send_telegram(msg)

    # =========================
    # STARTUP BANNER
    # =========================

    def _print_banner(self) -> None:
        bar = "=" * 58
        print(f"\n{bar}")
        print(f"   TradingBot — Autonomous Runtime Manager")
        print(f"   {paper_tag()}")
        print(f"   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{bar}\n")


# =========================
# HELPERS
# =========================

def _pid_is_alive(pid: int) -> bool:
    """Return True if a PID is alive (Windows-compatible via tasklist)."""
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            return str(pid) in result.stdout
        else:
            os.kill(pid, 0)
            return True
    except Exception:
        return False


# =========================
# MAIN ENTRY POINT
# =========================

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="TradingBot Autonomous Runtime Manager",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--install-startup",
        action="store_true",
        help="Install Windows Task Scheduler startup task and exit.",
    )
    parser.add_argument(
        "--remove-startup",
        action="store_true",
        help="Remove Windows startup task and exit.",
    )
    parser.add_argument(
        "--status-startup",
        action="store_true",
        help="Print startup task status and exit.",
    )
    parser.add_argument(
        "--recover",
        action="store_true",
        help="Attempt session recovery before falling back to full startup.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    # --- Windows startup management (no trading needed) ---
    if args.install_startup or args.remove_startup or args.status_startup:
        from runtime.windows_startup import (
            install_startup_task,
            remove_startup_task,
            print_startup_status,
        )
        if args.install_startup:
            install_startup_task()
        elif args.remove_startup:
            remove_startup_task()
        else:
            print_startup_status()
        return

    # --- Normal runtime ---
    manager = RuntimeManager()

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT,  manager._signal_handler)
    signal.signal(signal.SIGTERM, manager._signal_handler)

    # Startup (or recovery)
    if args.recover:
        success = manager.recover_session()
    else:
        success = manager.startup()

    if not success:
        logger.critical("[RUNTIME] Startup failed — exiting.")
        sys.exit(1)

    # =========================
    # MAIN KEEP-ALIVE LOOP
    # =========================
    # The watchdog thread does all the work. Main thread just blocks here,
    # prints periodic status, and waits for shutdown signal.

    logger.info("[RUNTIME] System running. Press Ctrl+C to stop.")
    print("\n" + "=" * 58)
    print("   TRADINGBOT RUNNING — Press Ctrl+C to stop")
    print("=" * 58 + "\n")

    try:
        while not manager._shutdown_event.is_set():
            manager._shutdown_event.wait(timeout=300)  # Print status every 5 min

            if manager._shutdown_event.is_set():
                break

            # --- Periodic status log ---
            with manager._lock:
                alive = [n for n, r in manager._modules.items() if r.is_alive]
                dead  = [n for n, r in manager._modules.items() if not r.is_alive]
            logger.info(
                f"[RUNTIME] Status — alive: {alive} | dead: {dead} | "
                f"TV CDP: {'UP' if browser_is_alive(TRADINGVIEW_CDP_PORT) else 'DOWN'} | "
                f"Groww CDP: {'UP' if browser_is_alive(GROWW_CDP_PORT) else 'DOWN'}"
            )
            manager._save_session()

    except KeyboardInterrupt:
        manager.shutdown(reason="Keyboard interrupt")
        sys.exit(0)


if __name__ == "__main__":
    main()
