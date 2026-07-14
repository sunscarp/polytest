#!/usr/bin/env python3
"""
Weather NO Simulator Dashboard Server
Serves the dashboard HTML and JSON API endpoints.
Reads live state from logs/simulator_state.json and per-market log files.
"""

import json
import os
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
from config import LOGS_DIR
from data_sources import polymarket

STATE_FILE = LOGS_DIR / "simulator_state.json"
PORT = int(os.environ.get("PORT", os.environ.get("DASHBOARD_PORT", 8081)))


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def get_state():
    state = read_json(STATE_FILE)
    if state is None:
        state = {
            "starting_bankroll": 10.0,
            "balance": 10.0,
            "open_positions": {},
            "closed_positions": [],
        }
    return state


def api_summary():
    state = get_state()
    balance = state.get("balance", 10.0)
    starting = state.get("starting_bankroll", 10.0)
    open_positions = state.get("open_positions", {})
    closed = state.get("closed_positions", [])

    total = len(closed)
    wins = sum(1 for p in closed if (p.get("pnl", 0) or 0) >= 0)
    losses = total - wins
    total_pnl = sum(p.get("pnl", 0) or 0 for p in closed)
    unrealized = 0.0
    for pos in open_positions.values():
        unrealized += pos.get("bet_size", 0)

    avg_hold = (sum(p.get("hold_time_hours", 0) or 0 for p in closed) / total) if total else 0

    # Build balance history from closed trades
    balance_history = [{"balance": starting, "ts": "", "label": "Start"}]
    running = starting
    for p in sorted(closed, key=lambda x: x.get("closed_at", "")):
        pnl = p.get("pnl", 0) or 0
        running += pnl
        label = f"{p.get('city_slug', '')} {p.get('date', '')}"
        balance_history.append({
            "balance": round(running, 2),
            "ts": p.get("closed_at", ""),
            "label": label,
        })

    return {
        "balance": balance,
        "starting_bankroll": starting,
        "total_return": round(balance - starting, 2),
        "total_return_pct": round((balance - starting) / starting * 100, 1) if starting else 0,
        "total_pnl": round(total_pnl, 2),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / total * 100, 1) if total else 0,
        "positions_opened": total + len(open_positions),
        "positions_closed": total,
        "positions_still_open": len(open_positions),
        "total_committed": round(unrealized, 2),
        "avg_hold_hours": round(avg_hold, 1),
        "balance_history": balance_history,
        "saved_at": state.get("saved_at", ""),
    }


def api_positions():
    state = get_state()
    open_positions = state.get("open_positions", {})
    positions = []

    for key, pos in open_positions.items():
        bucket = f"{pos.get('bucket_low', '?')}-{pos.get('bucket_high', '?')}"
        last_events = pos.get("monitoring_events", [])[-3:]
        last_event = last_events[-1] if last_events else None

        # Fetch live NO price from Polymarket API
        market_id = pos.get("market_id", "")
        live_price = pos.get("current_no_price", pos.get("entry_no_price", 0))
        if market_id:
            try:
                fetched = polymarket.get_market_price(market_id)
                if fetched is not None and isinstance(fetched, dict):
                    live_price = fetched.get("no_price", live_price)
            except Exception:
                pass

        positions.append({
            "key": key,
            "city_slug": pos.get("city_slug", ""),
            "date": pos.get("date", ""),
            "question": pos.get("question", ""),
            "bucket": bucket,
            "bet_size": pos.get("bet_size", 0),
            "entry_no_price": pos.get("entry_no_price", 0),
            "current_no_price": live_price,
            "shares": pos.get("shares", 0),
            "weather_com_high": pos.get("weather_com_high"),
            "open_meteo_high": pos.get("open_meteo_high"),
            "distance": pos.get("distance", 0),
            "market_id": market_id,
            "opened_at": pos.get("opened_at", ""),
            "last_monitored": pos.get("last_monitored", ""),
            "last_metar_temp": pos.get("last_metar_temp"),
            "last_wc_current": pos.get("last_wc_current"),
            "metar_distances": pos.get("metar_distances", []),
            "last_event": last_event,
            "monitoring_events": last_events,
        })

    return sorted(positions, key=lambda x: x["distance"], reverse=True)


def api_closed():
    state = get_state()
    closed = state.get("closed_positions", [])
    results = []
    for pos in closed:
        bucket = f"{pos.get('bucket_low', '?')}-{pos.get('bucket_high', '?')}"
        results.append({
            "key": f"{pos.get('city_slug', '')}_{pos.get('date', '')}",
            "city_slug": pos.get("city_slug", ""),
            "date": pos.get("date", ""),
            "bucket": bucket,
            "bet_size": pos.get("bet_size", 0),
            "entry_no_price": pos.get("entry_no_price", 0),
            "exit_no_price": pos.get("exit_no_price"),
            "pnl": pos.get("pnl", 0),
            "exit_reason": pos.get("exit_reason", ""),
            "hold_time_hours": pos.get("hold_time_hours", 0),
            "opened_at": pos.get("opened_at", ""),
            "closed_at": pos.get("closed_at", ""),
            "monitoring_events": pos.get("monitoring_events", []),
        })
    return sorted(results, key=lambda x: x.get("closed_at", ""), reverse=True)


def api_events():
    """Get all monitoring events from per-market log files."""
    events = []
    if not LOGS_DIR.exists():
        return events
    for f in sorted(LOGS_DIR.glob("*.json")):
        if f.name == "simulator_state.json":
            continue
        data = read_json(f)
        if not data or not isinstance(data, list):
            continue
        for evt in data:
            events.append(evt)
    return sorted(events, key=lambda x: x.get("ts", ""), reverse=True)[:100]


def api_timers():
    """Get bot timer state for dashboard countdown timers."""
    timer_file = LOGS_DIR / "timer_state.json"
    data = read_json(timer_file)
    if data is None:
        return {"last_full_scan": 0, "last_weather_poll": 0,
                "metar_poll_seconds": 45, "weather_poll_seconds": 600}
    return data


def api_regions():
    """Return current allowed regions and IST hour for dashboard display."""
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
        "summary": api_summary(),
        "positions": api_positions(),
        "closed": api_closed(),
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
            elif path == "/api/summary":
                self._json_response(api_summary())
            elif path == "/api/positions":
                self._json_response(api_positions())
            elif path == "/api/closed":
                self._json_response(api_closed())
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

    def _json_response(self, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
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
    print(f"    /api/summary    - balance, P/L, win rate")
    print(f"    /api/positions  - open positions")
    print(f"    /api/closed     - closed trade history")
    print(f"    /api/events     - monitoring events")
    print(f"    /api/all        - everything combined")
    print(f"  Ctrl+C to stop\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Dashboard stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
