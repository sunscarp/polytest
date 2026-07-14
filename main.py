#!/usr/bin/env python3
"""
Weather NO Trading Simulator — Main Orchestrator

Discovers Polymarket temperature markets, evaluates entry signals,
and monitors open positions using a weather.com + METAR cross-check strategy.

Usage:
    python main.py              # run strategy + monitoring loop
    python main.py scan         # one-shot scan (no monitoring loop)
    python main.py status       # show current state
    python main.py report       # full report of closed positions
"""

import sys
import json
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent))

from config import (
    METAR_POLL_SECONDS, WEATHER_POLL_SECONDS,
    MIN_VOLUME, LOGS_DIR,
)
from simulator import Simulator
from strategy import evaluate_entry, monitor_position
from data_sources import polymarket

import json as _json

# ── Logging ───────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")


# ── Station Loading ───────────────────────────────────────────────────────

def load_stations() -> dict:
    from config import STATIONS_FILE
    return json.loads(STATIONS_FILE.read_text(encoding="utf-8"))


# ── Market Discovery ──────────────────────────────────────────────────────

def get_allowed_regions() -> set:
    """
    Return set of allowed region strings based on current IST hour.
    12 AM – 8 AM IST  → Asia only
    8 AM – 3 PM IST   → Asia + Europe + Africa
    3 PM – 12 AM IST  → All regions
    """
    IST = timezone(timedelta(hours=5, minutes=30))
    hour = datetime.now(IST).hour
    if hour < 8:
        return {"asia"}
    elif hour < 15:
        return {"asia", "europe", "africa"}
    else:
        return {"asia", "europe", "africa", "americas"}


def discover_markets(stations: dict, skip_cities: set = None) -> list[dict]:
    """
    Find available Polymarket temperature markets for TODAY in IST only.
    Returns list of {city_slug, station, date_str, event_slug} dicts.
    Cities in skip_cities are not queried (no data available).
    Filters cities by region based on IST time window.
    """
    IST = timezone(timedelta(hours=5, minutes=30))
    today_ist = datetime.now(IST).strftime("%Y-%m-%d")
    markets = []
    if skip_cities is None:
        skip_cities = set()

    allowed_regions = get_allowed_regions()
    logger.info("IST date: %s | allowed regions: %s", today_ist, allowed_regions)

    for city_slug, station in stations.items():
        if city_slug in skip_cities:
            continue

        region = station.get("region", "asia")
        if region not in allowed_regions:
            continue

        event = polymarket.get_event(city_slug, today_ist)
        if event and event.get("markets"):
            # Check if any bucket has sufficient volume
            has_volume = any(
                float(m.get("volume", 0)) >= MIN_VOLUME
                for m in event.get("markets", [])
            )
            if has_volume:
                markets.append({
                    "city_slug": city_slug,
                    "station": station,
                    "date_str": today_ist,
                    "event_slug": event.get("slug", ""),
                    "market_count": len(event.get("markets", [])),
                })
                logger.info("Found market: %s %s (%d buckets)",
                            station["name"], today_ist,
                            len(event.get("markets", [])))
        else:
            # No event/buckets for this city today — skip in future scans
            skip_cities.add(city_slug)
            logger.info("No data for %s %s — skipping in future scans",
                        station["name"], today_ist)

        time.sleep(0.3)  # rate limit between cities

    return markets


# ── Entry Scan ────────────────────────────────────────────────────────────

def scan_entries(stations: dict, markets: list[dict], sim: Simulator) -> int:
    """
    Evaluate entry signals for all discovered markets.
    Collects ALL signals first, ranks by distance (best spread first),
    then opens positions from best to worst until bankroll is depleted.
    Returns number of positions opened.
    """
    # Phase 1: collect all signals
    all_signals = []
    for market in markets:
        city_slug = market["city_slug"]
        station = market["station"]
        date_str = market["date_str"]

        if sim.has_position(city_slug, date_str):
            continue

        signal = evaluate_entry(city_slug, station, date_str)
        if signal is not None:
            all_signals.append(signal)

    if not all_signals:
        return 0

    # Phase 2: rank by distance (largest spread = best NO opportunity)
    all_signals.sort(key=lambda s: s["distance"], reverse=True)

    # Phase 3: open from best to worst, respecting bankroll
    opened = 0
    for signal in all_signals:
        city_slug = signal["city_slug"]
        date_str = signal["date"]

        if sim.has_position(city_slug, date_str):
            continue

        pos = sim.open_position(
            city_slug=city_slug,
            date_str=date_str,
            bet_size=signal["bet_size"],
            entry_no_price=signal["no_price"],
            market_id=signal["market_id"],
            question=signal["question"],
            bucket_range=signal["bucket_range"],
            weather_com_high=signal["wc_high"],
            open_meteo_high=signal["om_high"],
            distance=signal["distance"],
        )
        if pos:
            opened += 1
            logger.info("OPENED (rank #%d): %s %s -- $%.2f NO @ $%.3f (dist=%.1f)",
                        all_signals.index(signal) + 1,
                        city_slug, date_str,
                        pos["bet_size"], pos["entry_no_price"], signal["distance"])

    return opened


# ── Position Monitoring ──────────────────────────────────────────────────

def monitor_all_positions(stations: dict, sim: Simulator) -> int:
    """
    Monitor all open positions. Returns number of positions closed.
    """
    closed = 0
    keys_to_check = list(sim.open_positions.keys())

    for key in keys_to_check:
        pos = sim.open_positions.get(key)
        if not pos:
            continue

        city_slug = pos["city_slug"]
        date_str = pos["date"]
        station = stations.get(city_slug)
        if not station:
            logger.warning("Unknown station for %s, skipping monitor", city_slug)
            continue

        # Get current market price for the position
        from data_sources import polymarket as pm
        price_data = pm.get_market_price(pos["market_id"])
        if price_data:
            current_no = price_data["no_price"]
            pos["current_no_price"] = current_no
            pos["last_monitored"] = datetime.now(timezone.utc).isoformat()

            # Check if market resolved
            if price_data.get("closed"):
                if price_data["yes_price"] >= 0.95:
                    sim.close_position(city_slug, date_str, "resolution_loss")
                elif price_data["yes_price"] <= 0.05:
                    sim.close_position(city_slug, date_str, "resolution_win")
                else:
                    sim.close_position(city_slug, date_str, "resolution_loss")
                closed += 1
                continue
        else:
            current_no = pos["entry_no_price"]
            pos["current_no_price"] = current_no
            pos["last_monitored"] = datetime.now(timezone.utc).isoformat()

        # Run monitoring strategy
        action = monitor_position(city_slug, station, date_str, pos, sim,
                                   current_no_price=current_no)

        if action == "sell":
            sim.close_position(city_slug, date_str, "monitor_sell", current_no)
            closed += 1
        elif action == "tighten":
            logger.info("Tightening monitoring for %s", key)

    # Persist updated prices
    if keys_to_check:
        sim.save_state()

    return closed


# ── Main Commands ─────────────────────────────────────────────────────────

def cmd_scan():
    """One-shot: discover markets, evaluate entries, print results."""
    stations = load_stations()
    sim = Simulator()

    IST = timezone(timedelta(hours=5, minutes=30))
    today_ist = datetime.now(IST).strftime("%Y-%m-%d")
    print(f"\n{'='*55}")
    print(f"  WEATHER NO SIMULATOR — SCAN")
    print(f"{'='*55}")
    print(f"  Balance:   ${sim.balance:.2f}")
    print(f"  Open:      {sim.open_count()}")
    print(f"  IST date:  {today_ist}")
    print(f"  Scanning {len(stations)} cities...\n")

    markets = discover_markets(stations)
    if not markets:
        print("  No markets found.")
        return

    print(f"\n  Found {len(markets)} markets. Evaluating entries...\n")
    opened = scan_entries(stations, markets, sim)

    print(f"\n  Scan complete: {opened} new position(s) opened")
    print(f"  Balance: ${sim.balance:.2f}")
    sim.print_summary()


def cmd_run():
    """Continuous loop: scan + monitor."""
    stations = load_stations()
    sim = Simulator()

    print(f"\n{'='*55}")
    print(f"  WEATHER NO SIMULATOR — RUNNING")
    print(f"{'='*55}")
    print(f"  Balance:      ${sim.balance:.2f}")
    print(f"  Open:         {sim.open_count()}")
    print(f"  METAR poll:   every {METAR_POLL_SECONDS}s")
    print(f"  Weather poll: every {WEATHER_POLL_SECONDS}s")
    print(f"  Ctrl+C to stop\n")

    last_full_scan = 0
    last_weather_poll = 0
    skip_cities = set()
    IST = timezone(timedelta(hours=5, minutes=30))
    current_ist_date = datetime.now(IST).strftime("%Y-%m-%d")

    while True:
        now_ts = time.time()
        ist_now = datetime.now(IST)
        now_str = ist_now.strftime("%H:%M:%S IST")

        try:
            # Full market scan every WEATHER_POLL_SECONDS
            if now_ts - last_full_scan >= WEATHER_POLL_SECONDS:
                # Reset skip list on new IST day
                today_ist = datetime.now(IST).strftime("%Y-%m-%d")
                if today_ist != current_ist_date:
                    skip_cities = set()
                    current_ist_date = today_ist

                print(f"[{now_str}] full scan for {today_ist}... ({len(skip_cities)} cities skipped, regions: {get_allowed_regions()})")
                markets = discover_markets(stations, skip_cities)
                opened = scan_entries(stations, markets, sim)
                last_full_scan = time.time()
                last_weather_poll = time.time()
                print(f"  balance: ${sim.balance:.2f} | open: {sim.open_count()} | new: {opened}")

            # Monitor positions every METAR_POLL_SECONDS
            elif now_ts - last_weather_poll >= METAR_POLL_SECONDS:
                if sim.open_count() > 0:
                    print(f"[{now_str}] monitoring {sim.open_count()} position(s)...")
                    closed = monitor_all_positions(stations, sim)
                    last_weather_poll = time.time()
                    print(f"  balance: ${sim.balance:.2f} | closed: {closed}")

        except KeyboardInterrupt:
            print(f"\n  Stopping — saving state...")
            sim.save_state()
            sim.print_summary()
            break
        except Exception as e:
            logger.error("Error in main loop: %s", e)
            time.sleep(30)
            continue

        # Write timer state for dashboard countdown timers
        try:
            timer_data = {
                "last_full_scan": last_full_scan,
                "last_weather_poll": last_weather_poll,
                "metar_poll_seconds": METAR_POLL_SECONDS,
                "weather_poll_seconds": WEATHER_POLL_SECONDS,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            (LOGS_DIR / "timer_state.json").write_text(
                _json.dumps(timer_data), encoding="utf-8"
            )
        except Exception:
            pass

        time.sleep(15)  # base tick every 15s


def cmd_status():
    """Show current state."""
    sim = Simulator()
    sim.print_summary()

    if sim.open_positions:
        print("  Open positions:")
        for key, pos in sim.open_positions.items():
            bucket = f"{pos['bucket_low']}-{pos['bucket_high']}"
            print(f"    {key}: $%.2f NO @ $%.3f | bucket %s | dist=%.1f°C | opened %s" %
                  (pos["bet_size"], pos["entry_no_price"], bucket,
                   pos["distance"], pos["opened_at"][:16]))
        print()


def cmd_report():
    """Full report of closed positions."""
    sim = Simulator()

    if not sim.closed_positions:
        print("  No closed positions yet.")
        return

    print(f"\n{'='*70}")
    print(f"  NO TRADING SIMULATOR — TRADE REPORT")
    print(f"{'='*70}")

    for pos in sorted(sim.closed_positions, key=lambda x: x.get("closed_at", "")):
        key = f"{pos['city_slug']}_{pos['date']}"
        bucket = f"{pos['bucket_low']}-{pos['bucket_high']}"
        pnl = pos.get("pnl", 0)
        reason = pos.get("exit_reason", "?")
        hold = pos.get("hold_time_hours", 0)
        result = "WIN" if pnl >= 0 else "LOSS"

        print(f"  {key:<30} {bucket:<12} ${pos['bet_size']:.2f} "
              f"NO@${pos['entry_no_price']:.3f} → "
              f"{'+'if pnl>=0 else ''}${pnl:.2f} "
              f"[{result}] {reason} ({hold:.1f}h)")

    print(f"\n{'='*70}")
    sim.print_summary()


# ── CLI ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"

    if cmd == "run":
        cmd_run()
    elif cmd == "scan":
        cmd_scan()
    elif cmd == "status":
        cmd_status()
    elif cmd == "report":
        cmd_report()
    else:
        print("Usage: python main.py [run|scan|status|report]")
