#!/usr/bin/env python3
"""
Weather NO Recommender Dashboard Server
Serves dashboard HTML and JSON API endpoints.
Shows buy/sell recommendations, real positions, paper trading, and monitoring events.
"""

import json
import os
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
from config import LOGS_DIR, PAPER_TRADING
from data_sources import polymarket
import trader
from simulator import Simulator

PORT = int(os.environ.get("PORT", os.environ.get("DASHBOARD_PORT", 8081)))

sim = Simulator()


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def api_balance():
    """Wallet balance no longer available (CLOB removed)."""
    return {"balance": None, "address": os.getenv("POLY_FUNDER_ADDRESS", "")}


def api_positions():
    """Fetch real on-chain positions enriched with market labels."""
    return trader.get_enriched_positions()


def api_buy_recommendations():
    """Get recent buy recommendations (no emails — dashboard only)."""
    return trader.load_buy_recommendations()


def api_sell_recommendations():
    """Get sell recommendations with email status."""
    return trader.load_sell_recommendations()


def api_paper():
    """Get paper trading state: bankroll, open positions, closed positions, summary."""
    return {
        "paper_trading": PAPER_TRADING,
        "summary": sim.summary(),
        "open_positions": sim.open_positions,
        "closed_positions": sim.closed_positions[-20:],
    }


def api_summary():
    """Machine-readable summary for cron health checks."""
    buy_recs = trader.load_buy_recommendations()
    sell_recs = trader.load_sell_recommendations()
    positions = trader.get_enriched_positions()

    from datetime import datetime, timezone
    result = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": "paper" if PAPER_TRADING else "recommendation",
        "positions_real": len(positions),
        "buy_recs": len(buy_recs),
        "sell_recs": len(sell_recs),
        "emails_sent": sum(1 for r in sell_recs if r.get("email_sent")),
    }

    if PAPER_TRADING:
        result["paper"] = sim.summary()

    return result


def api_events():
    """Get monitoring events."""
    events = []
    if not LOGS_DIR.exists():
        return events
    for f in sorted(LOGS_DIR.glob("*.json")):
        if f.name in ("simulator_state.json", "timer_state.json",
                       "buy_recommendations.json", "sell_recommendations.json",
                       "monitoring_state.json", "market_cache.json"):
            continue
        data = read_json(f)
        if not data or not isinstance(data, list):
            continue
        for evt in data:
            events.append(evt)
    return sorted(events, key=lambda x: x.get("ts", ""), reverse=True)[:100]


def api_timers():
    timer_file = LOGS_DIR / "timer_state.json"
    data = read_json(timer_file)
    if data is None:
        return {"last_full_scan": 0, "last_weather_poll": 0,
                "metar_poll_seconds": 45, "weather_poll_seconds": 600}
    return data


def api_regions():
    from datetime import datetime, timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    ist_now = datetime.now(IST)
    hour = ist_now.hour
    if hour < 8:
        allowed = {"asia"}
        window = "Asia Only"
    elif hour < 15:
        allowed = {"asia", "europe", "africa"}
        window = "Asia + Europe + Africa"
    else:
        allowed = {"asia", "europe", "africa", "americas"}
        window = "All Regions"
    return {
        "ist_hour": ist_now.strftime("%H:%M"),
        "window": window,
        "allowed_regions": sorted(allowed),
    }


def api_all():
    return {
        "balance": api_balance(),
        "positions": api_positions(),
        "buy_recommendations": api_buy_recommendations(),
        "sell_recommendations": api_sell_recommendations(),
        "paper": api_paper(),
        "events": api_events(),
        "timers": api_timers(),
        "regions": api_regions(),
    }


class DashboardHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        try:
            if path == "" or path == "/":
                self._serve_file("dashboard.html", "text/html")
            elif path == "/api/balance":
                self._json_response(api_balance())
            elif path == "/api/positions":
                self._json_response(api_positions())
            elif path == "/api/buy-recommendations":
                self._json_response(api_buy_recommendations())
            elif path == "/api/sell-recommendations":
                self._json_response(api_sell_recommendations())
            elif path == "/api/paper":
                self._json_response(api_paper())
            elif path == "/api/summary":
                self._json_response(api_summary())
            elif path == "/api/events":
                self._json_response(api_events())
            elif path == "/api/timers":
                self._json_response(api_timers())
            elif path == "/api/regions":
                self._json_response(api_regions())
            elif path == "/api/all":
                self._json_response(api_all())
            else:
                self.send_error(404)
        except Exception as e:
            try:
                self.send_error(500, str(e))
            except Exception:
                pass

    def do_POST(self):
        self.send_error(404)

    def _json_response(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, filename, content_type):
        filepath = Path(__file__).parent / filename
        if not filepath.exists():
            self.send_error(404, f"{filename} not found")
            return
        body = filepath.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def main():
    server = HTTPServer(("0.0.0.0", PORT), DashboardHandler)
    print(f"  Dashboard running at http://localhost:{PORT}")
    print(f"  API endpoints:")
    print(f"    /api/balance              - wallet balance")
    print(f"    /api/positions            - real on-chain positions")
    print(f"    /api/buy-recommendations  - buy signals (no email)")
    print(f"    /api/sell-recommendations - sell recs (emailed)")
    print(f"    /api/paper                - paper trading state")
    print(f"    /api/summary              - health check (for cron)")
    print(f"    /api/events               - monitoring events")
    print(f"    /api/all                  - everything combined")
    print(f"  Ctrl+C to stop\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Dashboard stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
