"""
core/trade_manager.py
=====================
Trade State Machine — manages the full lifecycle of one paper trade.

Responsibilities
----------------
- Persist trade state to data/trade_state.json (atomic writes)
- Track entry, SL, milestone hits, lot count, timestamps
- Compute and update trailing SL after each milestone
- Detect reversal (5m direction flip + EMA9 confirmation)
- Detect EOD forced close (15:29 IST)
- Expose clean methods for engine.py to consume

Target / Trailing SL Ladder (NIFTY points from entry)
------------------------------------------------------
  T1 = +25 → SL moves to breakeven (entry price)
  T2 = +40 → SL moves to T1 level  (entry + 25)
  T3 = +60 → SL moves to T2 level  (entry + 40)
  T4 = +85 → SL moves to T3 level  (entry + 60)
  T5 = +110 → SL moves to T4 level (entry + 85)
  ...each Tn+1 adds VIRTUAL_TARGET_STEP(25) to Tn

Reversal Detection
------------------
Close the trade when BOTH are true:
  1. 5-minute candle direction flips against the trade direction
  2. Price is on the wrong side of EMA9 (below EMA9 for longs, above for shorts)

Paper Trading Only — no real order execution happens here.
When PAPER_TRADING_MODE=False, entry and exit orders are placed via broker/order_manager.py.
"""

import json
import logging
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from pathlib import Path
from typing import Optional, Dict, List

from broker.order_manager import place_entry as _place_entry, place_exit as _place_exit
from config.settings import (
    STATE_FILE,
    STOP_LOSS_POINTS,
    TARGET_1_POINTS,
    TARGET_2_POINTS,
    TARGET_3_POINTS,
    VIRTUAL_TARGET_STEP,
    VIRTUAL_TARGET_MAX_LEVELS,
    PAPER_TRADING_MODE,
    get_target_points,
    EOD_CLOSE_HOUR,
    EOD_CLOSE_MINUTE,
    REVERSAL_REQUIRE_EMA_CONFIRM,
    SL_AFTER_T1_OFFSET,
    BOOKING_FRACTION,
    NIFTY_LOT_SIZE,
    OPTION_DELTA,
)

logger = logging.getLogger(__name__)

# =============================================================================
# STATUS CONSTANTS
# =============================================================================

STATUS_IDLE      = "IDLE"         # No active trade
STATUS_PENDING   = "PENDING"      # Signal fired, waiting for Telegram approval
STATUS_OPEN      = "OPEN"         # Approved and active
STATUS_CLOSED_SL = "CLOSED_SL"    # Stopped out
STATUS_CLOSED_T  = "CLOSED_TARGET"# Closed on a target milestone
STATUS_CLOSED_REV = "CLOSED_REVERSAL"  # Closed on reversal detection
STATUS_CLOSED_EOD = "CLOSED_EOD"  # Closed at EOD (15:29)

ACTIVE_STATUSES = frozenset({STATUS_PENDING, STATUS_OPEN})
CLOSED_STATUSES = frozenset({
    STATUS_CLOSED_SL, STATUS_CLOSED_T, STATUS_CLOSED_REV, STATUS_CLOSED_EOD
})

# =============================================================================
# TRADE RECORD DATACLASS
# =============================================================================

@dataclass
class TradeRecord:
    """One paper trade — serialised to data/trade_state.json."""

    trade_id:        str            = ""
    status:          str            = STATUS_IDLE

    # Signal info
    direction:       str            = ""    # BULLISH or BEARISH
    signal_text:     str            = ""    # "BUY 24050 CE"
    confidence:      int            = 0     # Adjusted score 0-100
    lots:            int            = 1

    # Price levels (all NIFTY index points — not option premium)
    entry_price:     float          = 0.0
    current_price:   float          = 0.0
    sl_price:        float          = 0.0   # Current (trailing) stop-loss level
    initial_sl:      float          = 0.0   # Snapshot of SL at entry

    # Milestone tracking
    last_milestone:  int            = 0     # Highest target hit (0 = none)
    milestones_hit:  List[int]      = field(default_factory=list)
    target_sequence: Dict[str,float] = field(default_factory=dict)  # {T1: price, T2: price, ...}

    # Timestamps
    signal_time:     str            = ""    # ISO string
    open_time:       str            = ""    # When trade was approved & opened
    close_time:      str            = ""    # When trade was closed
    close_reason:    str            = ""    # SL / Tn / REVERSAL / EOD

    # Telegram callback data
    tg_message_id:   Optional[int]  = None  # Message ID of approval message
    pending_action:  str            = ""    # "APPROVE" | "REJECT" | "SCALE" | ""

    # P&L (paper)
    pnl_points:      float          = 0.0   # Points captured (positive = profit)
    pnl_inr:         float          = 0.0   # Estimated INR P&L

    # Per-milestone booking breakdown (informational analytics)
    # Key: "T1", "T2", etc.
    # Value: {"pts": 25, "fraction": 0.333, "lots_booked": 0.67, "inr": 625.0, "price": 23846.85}
    milestone_bookings: Dict        = field(default_factory=dict)

    # Broker execution (live mode only — empty string in paper mode)
    broker_order_id:    str         = ""   # Groww order_id from place_entry()
    strike:             int         = 0    # ATM strike used
    option_type:        str         = ""   # "CE" or "PE"
    expiry:             str         = ""   # e.g. "29MAY2026"


# =============================================================================
# TRADE MANAGER
# =============================================================================

class TradeManager:
    """
    Thread-safe trade state machine.

    All state lives in self._trade (TradeRecord) + disk (trade_state.json).
    The engine.py threads call into this class — never manipulate state directly.

    Usage (from engine.py)
    ----------------------
        tm = TradeManager()

        # Signal loop
        if not tm.has_active_trade():
            tm.open_pending(signal, lots)

        # Tracker loop
        if tm.is_open():
            result = tm.update(current_price, tf_data)
            # result.action in {"SL_HIT","TARGET_HIT","REVERSAL","EOD","OK"}

        # Telegram poller
        tm.handle_approval(action)  # "APPROVE" | "REJECT" | "SCALE"
    """

    def __init__(self) -> None:
        self._lock  = threading.Lock()
        self._trade = TradeRecord()
        self._load()
        logger.info("[TradeManager] Initialised | status=%s", self._trade.status)

    # =========================================================================
    # STATE QUERIES
    # =========================================================================

    def has_active_trade(self) -> bool:
        """True if a trade is PENDING or OPEN."""
        return self._trade.status in ACTIVE_STATUSES

    def is_open(self) -> bool:
        """True only if trade has been approved and is OPEN."""
        return self._trade.status == STATUS_OPEN

    def is_pending(self) -> bool:
        """True if waiting for Telegram approval."""
        return self._trade.status == STATUS_PENDING

    def get_trade(self) -> TradeRecord:
        """Return a shallow copy of the current trade record."""
        with self._lock:
            import copy
            return copy.copy(self._trade)

    def get_status(self) -> str:
        return self._trade.status

    # =========================================================================
    # OPEN A PENDING TRADE (before Telegram approval)
    # =========================================================================

    def open_pending(
        self,
        signal: Dict,
        lots: int,
        tg_message_id: Optional[int] = None,
    ) -> TradeRecord:
        """
        Record a new PENDING trade from a signal dict.
        Call this immediately after sending the Telegram approval request.

        Parameters
        ----------
        signal : dict from SignalEngine.compute()
        lots   : number of lots (1 = standard, >1 if SCALE approved)
        """
        with self._lock:
            now       = datetime.now()
            trade_id  = now.strftime("%Y%m%d_%H%M%S")
            price     = signal.get("price") or 0.0
            direction = signal.get("direction", "")

            # Use ATR-based dynamic SL if available, else fall back to fixed SL
            sl_pts = float(signal.get("dynamic_sl_points") or STOP_LOSS_POINTS)
            sl_pts = max(sl_pts, 8.0)   # Absolute minimum safety net

            # Compute SL level
            if direction == "BULLISH":
                sl_price = price - sl_pts
            else:
                sl_price = price + sl_pts

            self._trade = TradeRecord(
                trade_id       = trade_id,
                status         = STATUS_PENDING,
                direction      = direction,
                signal_text    = signal.get("trade_signal", ""),
                confidence     = signal.get("adjusted_score", 0),
                lots           = lots,
                entry_price    = price,
                current_price  = price,
                sl_price       = sl_price,
                initial_sl     = sl_price,
                signal_time    = now.isoformat(),
                tg_message_id  = tg_message_id,
                pending_action = "",
                # Broker fields
                strike         = int(signal.get("strike", 0)),
                option_type    = signal.get("option_type", "CE" if direction == "BULLISH" else "PE"),
                expiry         = signal.get("expiry", ""),
            )
            self._save()

        # ── Broker execution hook ─────────────────────────────────────────────
        # Paper mode: logs only. Live mode: places real BUY order on Groww.
        try:
            order_result = _place_entry(signal, lots)
            self._trade.broker_order_id = order_result.get("order_id", "")
            self._save()
        except Exception as _exc:
            logger.warning("[TradeManager] order_manager.place_entry failed: %s", _exc)

        logger.info(
            "[TradeManager] PENDING | id=%s dir=%s entry=%.2f SL=%.2f lots=%d",
            self._trade.trade_id, direction, price, sl_price, lots,
        )
        return self.get_trade()

    # =========================================================================
    # TELEGRAM APPROVAL HANDLING
    # =========================================================================

    def handle_approval(self, action: str) -> str:
        """
        Process a Telegram approval callback.

        Parameters
        ----------
        action : "APPROVE" | "REJECT" | "SCALE"

        Returns
        -------
        str — the resulting action, for the engine to act on
        """
        with self._lock:
            if self._trade.status != STATUS_PENDING:
                logger.warning(
                    "[TradeManager] Approval '%s' received but trade is not PENDING (status=%s)",
                    action, self._trade.status
                )
                return "IGNORED"

            if action == "REJECT":
                self._trade.status       = STATUS_IDLE
                self._trade.close_reason = "REJECTED"
                self._save()
                logger.info("[TradeManager] Trade REJECTED via Telegram")
                return "REJECTED"

            elif action in ("APPROVE", "SCALE"):
                if action == "SCALE":
                    self._trade.lots = min(self._trade.lots * 2, 5)
                    logger.info("[TradeManager] SCALED to %d lots", self._trade.lots)

                now = datetime.now()
                self._trade.status    = STATUS_OPEN
                self._trade.open_time = now.isoformat()
                # Recompute SL at open time price (keep same offset)
                self._save()
                logger.info(
                    "[TradeManager] Trade APPROVED | id=%s lots=%d",
                    self._trade.trade_id, self._trade.lots,
                )
                return "APPROVED"

            else:
                logger.warning("[TradeManager] Unknown approval action: %s", action)
                return "IGNORED"

    # =========================================================================
    # LIVE TRACKER UPDATE
    # Called every TRACKER_INTERVAL_SECONDS with fresh price + 5m TF data
    # =========================================================================

    def update(self, current_price: float, tf_data: Optional[Dict] = None) -> Dict:
        """
        Evaluate current price against SL, targets, reversal, and EOD.

        Parameters
        ----------
        current_price : float — latest NIFTY index price
        tf_data       : dict  — from DataEngine.get_analysis()["timeframe_data"]
                        Used for reversal detection (direction + EMA9)

        Returns
        -------
        dict with keys:
          action  : "SL_HIT" | "TARGET_HIT" | "REVERSAL" | "EOD" | "OK"
          target_n: int — target number hit (only when action="TARGET_HIT")
          message : str — human-readable summary
        """
        with self._lock:
            if self._trade.status != STATUS_OPEN:
                return {"action": "OK", "message": "Trade not OPEN"}

            self._trade.current_price = current_price
            direction = self._trade.direction
            entry     = self._trade.entry_price
            sl        = self._trade.sl_price

            # ── EOD check ────────────────────────────────────────────────────
            now = datetime.now()
            if now.hour > EOD_CLOSE_HOUR or (
                now.hour == EOD_CLOSE_HOUR and now.minute >= EOD_CLOSE_MINUTE
            ):
                return self._close_trade(
                    close_price  = current_price,
                    status       = STATUS_CLOSED_EOD,
                    close_reason = "EOD_15:29",
                )

            # ── SL check ────────────────────────────────────────────────────
            sl_hit = (
                (direction == "BULLISH" and current_price <= sl) or
                (direction == "BEARISH" and current_price >= sl)
            )
            if sl_hit:
                return self._close_trade(
                    close_price  = current_price,
                    status       = STATUS_CLOSED_SL,
                    close_reason = f"SL_HIT @ {current_price:.2f}",
                )

            # ── Target checks ────────────────────────────────────────────────
            # Check from the next milestone upward
            next_target_n = self._trade.last_milestone + 1
            for n in range(next_target_n, VIRTUAL_TARGET_MAX_LEVELS + 4):
                pts      = get_target_points(n)
                tgt_price = (
                    entry + pts if direction == "BULLISH"
                    else entry - pts
                )
                target_hit = (
                    (direction == "BULLISH" and current_price >= tgt_price) or
                    (direction == "BEARISH" and current_price <= tgt_price)
                )
                if target_hit:
                    return self._hit_target(n, current_price)
                else:
                    break   # Targets must be hit in order — stop at first miss

            # ── Reversal check (only after at least T1 hit) ─────────────────
            if self._trade.last_milestone >= 1 and tf_data is not None:
                reversal = self._check_reversal(direction, current_price, tf_data)
                if reversal:
                    return self._close_trade(
                        close_price  = current_price,
                        status       = STATUS_CLOSED_REV,
                        close_reason = f"REVERSAL @ {current_price:.2f}",
                    )

            # ── All checks passed — update price and save ────────────────────
            self._save()
            pts_from_entry = (
                current_price - entry if direction == "BULLISH"
                else entry - current_price
            )
            return {
                "action":  "OK",
                "message": (
                    f"OPEN | price={current_price:.2f} entry={entry:.2f} "
                    f"P&L={pts_from_entry:+.1f}pts SL={sl:.2f} "
                    f"last_milestone=T{self._trade.last_milestone}"
                ),
            }

    # =========================================================================
    # ABORT PENDING (timeout or system restart)
    # =========================================================================

    def abort_pending(self) -> None:
        """Reset a stale PENDING trade back to IDLE."""
        with self._lock:
            if self._trade.status == STATUS_PENDING:
                logger.info(
                    "[TradeManager] Aborting stale PENDING trade %s",
                    self._trade.trade_id,
                )
                self._trade.status       = STATUS_IDLE
                self._trade.close_reason = "TIMEOUT"
                self._save()

    # =========================================================================
    # INTERNAL HELPERS
    # =========================================================================

    def _hit_target(self, n: int, price: float) -> Dict:
        """Record a milestone hit and update trailing SL."""
        entry     = self._trade.entry_price
        direction = self._trade.direction
        pts       = get_target_points(n)

        # Update milestone list
        self._trade.last_milestone = n
        if n not in self._trade.milestones_hit:
            self._trade.milestones_hit.append(n)
        self._trade.target_sequence[f"T{n}"] = price

        # ── Trailing SL update ────────────────────────────────────────────────
        # After T1 → SL to entry + SL_AFTER_T1_OFFSET (BE+15, locks in min profit)
        # After T2 → SL to T1 level  (entry + 25)
        # After Tn → SL to T(n-1) level
        if n == 1:
            new_sl_offset = SL_AFTER_T1_OFFSET   # +15 above entry (not just breakeven)
        else:
            new_sl_offset = get_target_points(n - 1)

        if direction == "BULLISH":
            new_sl = entry + new_sl_offset
        else:
            new_sl = entry - new_sl_offset

        old_sl = self._trade.sl_price
        self._trade.sl_price = new_sl

        # ── Partial booking analytics ──────────────────────────────────────────
        # T1 and T2: track 1/3 position booking for display purposes
        # T3+: no further booking recorded — remainder runs with trailing SL
        if n <= 2:
            fraction    = BOOKING_FRACTION          # 1/3
            lots_booked = round(self._trade.lots * fraction, 4)
            booking_inr = round(pts * OPTION_DELTA * NIFTY_LOT_SIZE * lots_booked, 2)
        else:
            fraction    = 0.0
            lots_booked = 0.0
            booking_inr = 0.0

        self._trade.milestone_bookings[f"T{n}"] = {
            "pts":         pts,
            "fraction":    round(fraction, 3),
            "lots_booked": lots_booked,
            "inr":         booking_inr,
            "price":       price,
        }

        self._save()

        logger.info(
            "[TradeManager] T%d HIT @ %.2f | SL: %.2f → %.2f (offset +%d) | booked %.4f lots ₹%.0f",
            n, price, old_sl, new_sl, new_sl_offset, lots_booked, booking_inr,
        )
        return {
            "action":            "TARGET_HIT",
            "target_n":          n,
            "new_sl":            new_sl,
            "new_sl_offset":     new_sl_offset,
            "lots_booked":       lots_booked,
            "booking_inr":       booking_inr,
            "message":           (
                f"T{n} HIT @ {price:.2f} | new SL={new_sl:.2f} | "
                f"pts from entry={pts} | booked {lots_booked:.2f} lots ₹{booking_inr:.0f}"
            ),
        }

    def _check_reversal(
        self,
        direction: str,
        current_price: float,
        tf_data: Dict,
    ) -> bool:
        """
        Reversal detected when:
          1. 5m TF direction has flipped against the trade
          2. (If REVERSAL_REQUIRE_EMA_CONFIRM) price is on the wrong side of EMA9

        Returns True if reversal conditions are fully met.
        """
        primary_data  = tf_data.get("5m", {})
        tf_direction  = primary_data.get("direction", "")
        ema9          = primary_data.get("ema9")

        # Condition 1: direction flip
        if direction == "BULLISH":
            direction_flipped = tf_direction == "BEARISH"
        else:
            direction_flipped = tf_direction == "BULLISH"

        if not direction_flipped:
            return False

        # Condition 2: EMA9 confirmation (optional, controlled by settings)
        if REVERSAL_REQUIRE_EMA_CONFIRM and ema9 is not None:
            if direction == "BULLISH" and current_price > ema9:
                # Price still above EMA9 — no reversal yet
                return False
            if direction == "BEARISH" and current_price < ema9:
                # Price still below EMA9 — no reversal yet
                return False

        logger.info(
            "[TradeManager] REVERSAL detected | dir=%s tf_dir=%s price=%.2f ema9=%s",
            direction, tf_direction, current_price, f"{ema9:.2f}" if ema9 else "N/A",
        )
        return True

    def _close_trade(self, close_price: float, status: str, close_reason: str) -> Dict:
        """Finalise a trade — compute P&L, set status, save."""
        entry     = self._trade.entry_price
        direction = self._trade.direction
        lots      = self._trade.lots

        if direction == "BULLISH":
            pnl_pts = close_price - entry
        else:
            pnl_pts = entry - close_price

        # Approximate INR P&L using lot size and delta
        from config.settings import NIFTY_LOT_SIZE, OPTION_DELTA
        pnl_inr = pnl_pts * OPTION_DELTA * NIFTY_LOT_SIZE * lots

        self._trade.status       = status
        self._trade.close_time   = datetime.now().isoformat()
        self._trade.close_reason = close_reason
        self._trade.pnl_points   = round(pnl_pts, 2)
        self._trade.pnl_inr      = round(pnl_inr, 2)

        self._save()

        # ── Broker execution hook ─────────────────────────────────────────────
        # Paper mode: logs only. Live mode: places real SELL (square-off) on Groww.
        try:
            _place_exit(self._trade, reason=close_reason)
        except Exception as _exc:
            logger.warning("[TradeManager] order_manager.place_exit failed: %s", _exc)

        action_map = {
            STATUS_CLOSED_SL:  "SL_HIT",
            STATUS_CLOSED_T:   "TARGET_HIT",
            STATUS_CLOSED_REV: "REVERSAL",
            STATUS_CLOSED_EOD: "EOD",
        }
        logger.info(
            "[TradeManager] CLOSED | reason=%s pnl=%.2f pts / ₹%.0f",
            close_reason, pnl_pts, pnl_inr,
        )
        return {
            "action":      action_map.get(status, "CLOSED"),
            "close_reason": close_reason,
            "pnl_points":  pnl_pts,
            "pnl_inr":     pnl_inr,
            "message":     f"CLOSED {close_reason} @ {close_price:.2f} | P&L={pnl_pts:+.1f}pts / ₹{pnl_inr:+.0f}",
        }

    # =========================================================================
    # PERSISTENCE
    # =========================================================================

    def _save(self) -> None:
        """Atomically write trade state to data/trade_state.json."""
        try:
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(asdict(self._trade), indent=2, default=str),
                encoding="utf-8",
            )
            tmp.replace(STATE_FILE)
        except Exception as exc:
            logger.error("[TradeManager] Failed to save state: %s", exc)

    def _load(self) -> None:
        """Load persisted state from data/trade_state.json (if it exists)."""
        if not STATE_FILE.exists():
            logger.info("[TradeManager] No state file found — starting fresh")
            return

        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            self._trade = TradeRecord(**{
                k: v for k, v in data.items()
                if k in TradeRecord.__dataclass_fields__
            })
            logger.info(
                "[TradeManager] Loaded state | id=%s status=%s",
                self._trade.trade_id, self._trade.status,
            )

            # If we loaded a PENDING trade, it may be stale — abort it
            if self._trade.status == STATUS_PENDING:
                logger.warning(
                    "[TradeManager] Stale PENDING trade found on load — resetting to IDLE"
                )
                self._trade.status       = STATUS_IDLE
                self._trade.close_reason = "STALE_PENDING_ON_RESTART"
                self._save()

        except Exception as exc:
            logger.error(
                "[TradeManager] Failed to load state: %s — starting fresh", exc
            )
            self._trade = TradeRecord()

    def reset(self) -> None:
        """
        Reset trade state to IDLE.
        Call this after a trade closes and analytics have been logged.
        """
        with self._lock:
            self._trade = TradeRecord()
            self._save()
        logger.info("[TradeManager] State reset to IDLE")
                encoding="utf-8",
            )
            tmp.replace(STATE_FILE)
        except Exception as exc:
            logger.error("[TradeManager] Failed to save state: %s", exc)

    def _load(self) -> None:
        """Load persisted state from data/trade_state.json (if it exists)."""
        if not STATE_FILE.exists():
            logger.info("[TradeManager] No state file found -- starting fresh")
            return

        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            self._trade = TradeRecord(**{
                k: v for k, v in data.items()
                if k in TradeRecord.__dataclass_fields__
            })
            logger.info(
                "[TradeManager] Loaded state | id=%s status=%s",
                self._trade.trade_id, self._trade.status,
            )

            # If we loaded a PENDING trade, it may be stale -- abort it
            if self._trade.status == STATUS_PENDING:
                logger.warning(
                    "[TradeManager] Stale PENDING trade found on load -- resetting to IDLE"
                )
                self._trade.status       = STATUS_IDLE
                self._trade.close_reason = "STALE_PENDING_ON_RESTART"
                self._save()

        except Exception as exc:
            logger.error(
                "[TradeManager] Failed to load state: %s -- starting fresh", exc
            )
            self._trade = TradeRecord()

    def reset(self) -> None:
        """
        Reset trade state to IDLE.
        Call this after a trade closes and analytics have been logged.
        """
        with self._lock:
            self._trade = TradeRecord()
            self._save()
        logger.info("[TradeManager] State reset to IDLE")
