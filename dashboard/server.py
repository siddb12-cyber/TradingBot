"""
dashboard/server.py
===================
Live dashboard HTTP server for TradingBot.

Serves a single-page HTML dashboard on http://localhost:5050

Endpoints
---------
  GET /               -> Serve dashboard/index.html
  GET /api/trades     -> JSON list of closed trades (filtered by period)
  GET /api/decisions  -> JSON list of signal decisions
  GET /api/live       -> JSON current trade state + NIFTY price
  GET /api/stats      -> JSON summary stats (win rate, P&L, streaks)

Started automatically by main.py in a background daemon thread.
Can also be run standalone: python dashboard/server.py
"""

import json
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any

# Add project root to path so imports work when run directly
sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS

from config.settings import DECISIONS_DIR, TRADES_DIR, DATA_DIR, BASE_DIR

# =============================================================================
# FLASK APP
# =============================================================================

app = Flask(__name__, static_folder=str(Path(__file__).parent))
CORS(app)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

DASHBOARD_DIR = Path(__file__).parent
STATE_FILE    = DATA_DIR / "trade_state.json"

# =============================================================================
# HELPERS
# =============================================================================

def _load_json_dir(folder: Path, start_date: date, end_date: date) -> List[Dict]:
    """Load and merge all JSON files in folder within the date range."""
    results = []
    if not folder.exists():
        return results

    current = start_date
    while current <= end_date:
        filepath = folder / (current.strftime("%Y-%m-%d") + ".json")
        if filepath.exists():
            try:
                data = json.loads(filepath.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    results.extend(data)
            except Exception as exc:
                log.error("Error reading %s: %s", filepath, exc)
        current += timedelta(days=1)
    return results


def _parse_period(period: str):
    """Convert period string to (start_date, end_date)."""
    today = date.today()
    if period == "daily":
        return today, today
    elif period == "weekly":
        start = today - timedelta(days=today.weekday())
        return start, today
    elif period == "monthly":
        start = today.replace(day=1)
        return start, today
    elif period == "quarterly":
        q_month = ((today.month - 1) // 3) * 3 + 1
        start = today.replace(month=q_month, day=1)
        return start, today
    elif period == "half-yearly":
        h_month = 1 if today.month <= 6 else 7
        start = today.replace(month=h_month, day=1)
        return start, today
    elif period == "yearly":
        start = today.replace(month=1, day=1)
        return start, today
    else:
        return today - timedelta(days=30), today


def _compute_stats(trades: List[Dict]) -> Dict:
    """Compute aggregate statistics from a list of closed trade dicts."""
    if not trades:
        return {
            "total_trades": 0, "win_rate": 0.0,
            "total_pnl_points": 0.0, "total_pnl_inr": 0.0,
            "avg_pnl_points": 0.0, "best_trade": 0.0, "worst_trade": 0.0,
            "sl_count": 0, "t1_count": 0, "t2_count": 0, "t3_count": 0,
            "t4plus_count": 0, "reversal_count": 0, "eod_count": 0,
            "max_consecutive_wins": 0, "max_consecutive_losses": 0,
        }

    total        = len(trades)
    pnl_list     = [t.get("pnl_points", 0) for t in trades]
    winning      = [p for p in pnl_list if p > 0]
    win_rate     = (len(winning) / total * 100) if total else 0

    total_pnl_pts = sum(pnl_list)
    total_pnl_inr = sum(t.get("pnl_inr", 0) for t in trades)

    def _milestone_count(n: int) -> int:
        return sum(1 for t in trades if n in (t.get("milestones_hit") or []))

    def _reason_count(keyword: str) -> int:
        return sum(1 for t in trades if keyword in (t.get("close_reason") or ""))

    max_c_wins = max_c_losses = cur_w = cur_l = 0
    for p in pnl_list:
        if p > 0:
            cur_w += 1; cur_l = 0
            max_c_wins = max(max_c_wins, cur_w)
        else:
            cur_l += 1; cur_w = 0
            max_c_losses = max(max_c_losses, cur_l)

    return {
        "total_trades":          total,
        "win_rate":              round(win_rate, 1),
        "total_pnl_points":      round(total_pnl_pts, 2),
        "total_pnl_inr":         round(total_pnl_inr, 2),
        "avg_pnl_points":        round(total_pnl_pts / total, 2) if total else 0,
        "best_trade":            round(max(pnl_list), 2) if pnl_list else 0,
        "worst_trade":           round(min(pnl_list), 2) if pnl_list else 0,
        "sl_count":              _reason_count("SL_HIT"),
        "t1_count":              _milestone_count(1),
        "t2_count":              _milestone_count(2),
        "t3_count":              _milestone_count(3),
        "t4plus_count":          _milestone_count(4),
        "reversal_count":        _reason_count("REVERSAL"),
        "eod_count":             _reason_count("EOD"),
        "max_consecutive_wins":  max_c_wins,
        "max_consecutive_losses":max_c_losses,
    }


# =============================================================================
# ROUTES
# =============================================================================

@app.route("/")
def index():
    """Serve the dashboard HTML."""
    return send_from_directory(str(DASHBOARD_DIR), "index.html")


@app.route("/api/trades")
def api_trades():
    """Return closed trades for the given period."""
    period     = request.args.get("period", "weekly")
    start, end = _parse_period(period)
    trades     = _load_json_dir(TRADES_DIR, start, end)
    return jsonify({"trades": trades, "period": period, "start": str(start), "end": str(end)})


@app.route("/api/decisions")
def api_decisions():
    """Return signal decisions for the given period."""
    period     = request.args.get("period", "daily")
    start, end = _parse_period(period)
    decisions  = _load_json_dir(DECISIONS_DIR, start, end)
    return jsonify({"decisions": decisions, "period": period})


@app.route("/api/live")
def api_live():
    """Return current live trade state."""
    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    # get_live_price() returns Optional[float] directly
    live_price = None
    try:
        from core.data_engine import DataEngine
        live_price = DataEngine().get_live_price()
    except Exception:
        pass

    return jsonify({"trade": state, "live_price": live_price, "timestamp": datetime.now().isoformat()})


@app.route("/api/stats")
def api_stats():
    """Return aggregate statistics for the given period."""
    period     = request.args.get("period", "weekly")
    start, end = _parse_period(period)
    trades     = _load_json_dir(TRADES_DIR, start, end)
    stats      = _compute_stats(trades)
    return jsonify({"stats": stats, "period": period})


# =============================================================================
# ENTRY POINT (standalone use)
# =============================================================================

if __name__ == "__main__":
    port = int(os.getenv("DASHBOARD_PORT", "5050"))
    log.info("[Dashboard] Starting on http://localhost:%d", port)
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
