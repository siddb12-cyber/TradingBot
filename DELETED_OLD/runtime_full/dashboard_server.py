"""
runtime/dashboard_server.py
============================
Lightweight HTTP server for the TradingBot live dashboard.

Serves:
  GET /                          → live_dashboard.html
  GET /analytics                 → analytics_dashboard.html
  GET /api/status                → data/live_status.json (live feed)
  GET /api/archive               → data/archive/index.json (all archived days)
  GET /api/archive/YYYY-MM-DD    → data/archive/YYYY-MM-DD.json (one day)
  GET /live_dashboard.html       → same as /
  GET /analytics_dashboard.html  → same as /analytics

No pip installs required — uses Python built-in http.server only.

Usage:
  python runtime/dashboard_server.py
  # Then open http://localhost:8765 in your browser

Or launch silently via start_dashboard.vbs (no terminal window).

Port:  8765 (change DASHBOARD_PORT below if needed)
Host:  127.0.0.1 (localhost only — not exposed to network)
"""

import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# =========================
# CONFIG
# =========================

DASHBOARD_PORT = 8765
DASHBOARD_HOST = "127.0.0.1"

# Resolve BASE_DIR = TradingBot/  regardless of cwd
BASE_DIR   = Path(__file__).parent.parent.resolve()
DATA_DIR   = BASE_DIR / "data"
STATUS_FILE   = DATA_DIR / "live_status.json"
ARCHIVE_DIR   = DATA_DIR / "archive"
HTML_FILE     = BASE_DIR / "live_dashboard.html"
ANALYTICS_FILE = BASE_DIR / "analytics_dashboard.html"

# =========================
# LOGGING
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | DashboardServer | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# =========================
# REQUEST HANDLER
# =========================

class DashboardHandler(BaseHTTPRequestHandler):
    """Handles GET requests for the dashboard and API."""

    # Suppress access log spam — only log errors
    def log_message(self, format, *args):  # noqa: A002
        pass

    def do_GET(self):
        path = self.path.split("?")[0]   # strip query params

        # ---- API: live status JSON ----
        if path == "/api/status":
            self._serve_json()

        # ---- API: archive index (all days) ----
        elif path == "/api/archive":
            self._serve_archive_index()

        # ---- API: single day archive e.g. /api/archive/2026-05-22 ----
        elif path.startswith("/api/archive/") and len(path) == len("/api/archive/YYYY-MM-DD"):
            date_str = path[len("/api/archive/"):]
            self._serve_archive_day(date_str)

        # ---- Analytics dashboard ----
        elif path in ("/analytics", "/analytics_dashboard.html"):
            self._serve_analytics_html()

        # ---- Live Dashboard HTML ----
        elif path in ("/", "/live_dashboard.html", "/index.html"):
            self._serve_html()

        # ---- 404 ----
        else:
            self._send_404()

    # ---- Serve live_status.json ----
    def _serve_json(self):
        try:
            if STATUS_FILE.exists():
                with open(STATUS_FILE, "r", encoding="utf-8") as f:
                    body = f.read().encode("utf-8")
            else:
                # Return a minimal "not started" payload
                body = json.dumps({
                    "bot_status":   "NOT STARTED",
                    "engine_state": "IDLE",
                    "last_updated": None,
                    "current_price": None,
                    "active_trade": None,
                    "daily": {
                        "trades_today": 0,
                        "daily_pnl_points": 0,
                        "daily_pnl_inr": 0,
                        "consecutive_losses": 0,
                        "max_trades": 3,
                        "capital": 5000,
                    },
                    "config": {
                        "sl_pts": 25, "t1_pts": 35,
                        "t2_pts": 60, "t3_pts": 100,
                        "lot_size": 75, "delta": 0.5, "capital": 5000,
                    },
                    "scan_history": [],
                    "last_scan": {},
                }).encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        except Exception as exc:
            logger.error("Error serving /api/status: %s", exc)
            self._send_500()

    # ---- Serve archive index (data/archive/index.json) ----
    def _serve_archive_index(self):
        try:
            index_path = ARCHIVE_DIR / "index.json"
            if index_path.exists():
                with open(index_path, "r", encoding="utf-8") as f:
                    body = f.read().encode("utf-8")
            else:
                body = json.dumps({"days": []}).encode("utf-8")
            self._send_json_response(body)
        except Exception as exc:
            logger.error("Error serving /api/archive: %s", exc)
            self._send_500()

    # ---- Serve single archive day (data/archive/YYYY-MM-DD.json) ----
    def _serve_archive_day(self, date_str: str):
        try:
            # Validate date format to prevent path traversal
            import re
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
                self._send_404()
                return
            day_path = ARCHIVE_DIR / f"{date_str}.json"
            if day_path.exists():
                with open(day_path, "r", encoding="utf-8") as f:
                    body = f.read().encode("utf-8")
                self._send_json_response(body)
            else:
                body = json.dumps({"error": f"No archive for {date_str}"}).encode("utf-8")
                self.send_response(404)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
        except Exception as exc:
            logger.error("Error serving /api/archive/%s: %s", date_str, exc)
            self._send_500()

    def _send_json_response(self, body: bytes):
        """Helper: send a 200 JSON response with no-cache headers."""
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    # ---- Serve analytics dashboard HTML ----
    def _serve_analytics_html(self):
        try:
            if ANALYTICS_FILE.exists():
                with open(ANALYTICS_FILE, "r", encoding="utf-8") as f:
                    body = f.read().encode("utf-8")
            else:
                body = b"<h1>analytics_dashboard.html not found</h1><p>It will appear after the first trading day is archived.</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            logger.error("Error serving analytics HTML: %s", exc)
            self._send_500()

    # ---- Serve live Dashboard HTML ----
    def _serve_html(self):
        try:
            if HTML_FILE.exists():
                with open(HTML_FILE, "r", encoding="utf-8") as f:
                    body = f.read().encode("utf-8")
            else:
                body = b"<h1>live_dashboard.html not found</h1><p>Place it in TradingBot/ root.</p>"

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        except Exception as exc:
            logger.error("Error serving dashboard HTML: %s", exc)
            self._send_500()

    def _send_404(self):
        body = b"<h1>404 Not Found</h1>"
        self.send_response(404)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_500(self):
        body = b"<h1>500 Server Error</h1>"
        self.send_response(500)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# =========================
# MAIN
# =========================

def main():
    server = HTTPServer((DASHBOARD_HOST, DASHBOARD_PORT), DashboardHandler)
    url = f"http://{DASHBOARD_HOST}:{DASHBOARD_PORT}"
    logger.info("Dashboard server starting at %s", url)
    logger.info("  Live dashboard : %s/", url)
    logger.info("  Analytics      : %s/analytics", url)
    logger.info("  API - status   : %s/api/status", url)
    logger.info("  API - archive  : %s/api/archive", url)
    logger.info("Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Dashboard server stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
