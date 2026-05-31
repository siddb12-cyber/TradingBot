"""
broker/
=======
Execution layer for live order placement via Groww API.

Structure
---------
groww_client.py  — raw HTTP wrapper around Groww REST API (stub until credentials arrive)
order_manager.py — routes orders through PAPER_TRADING_MODE gate

Usage
-----
Paper mode  (PAPER_TRADING_MODE=True)  : orders are logged only, no HTTP calls made
Live mode   (PAPER_TRADING_MODE=False) : orders are placed via groww_client

To go live:
  1. Add GROWW_API_KEY + GROWW_ACCESS_TOKEN to .env
  2. Fill in TODO sections in groww_client.py with real HTTP calls
  3. Set PAPER_TRADING_MODE=False in config/settings.py (requires explicit sign-off)
  4. Test with 1 lot on a live but small position
"""
