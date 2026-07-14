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
import trader
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

def scan_entries(stations: dict, markets: list[dict], state: dict) -> int:
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

        key = f"{city_slug}_{date_str}"
        if key in state["open_positions"]:
            continue

        signal = evaluate_entry(city_slug, station, date_str)
        if signal is not None:
            all_signals.append(signal)

    if not all_signals:
        return 0

    # Phase 2: rank by distance (largest spread = best NO opportunity)
    all_signals.sort(key=lambda s: s["distance"], reverse=True)

    # Phase 3: open from best to worst, respecting balance
    opened = 0
    for signal in all_signals:
        city_slug = signal["city_slug"]
        date_str = signal["date"]
        key = f"{city_slug}_{date_str}"

        if key in state["open_positions"]:
            continue

        bet_size = signal["bet_size"]

        # Check balance
        if bet_size > state["balance"]:
            logger.info("Insufficient balance: $%.2f < $%.2f bet", state["balance"], bet_size)
            continue

        # Place real order on CLOB
        token_id = signal.get("token_id", "")
        if not token_id:
            logger.warning("No token_id for %s/%s, skipping", city_slug, date_str)
            continue

        no_price = signal["no_price"]
        neg_risk = signal.get("neg_risk", False)

        # Calculate shares: bet_size / no_price
        shares = round(bet_size / no_price, 2) if no_price > 0 else 0
        if shares <= 0:
            continue

        logger.info("Placing LIVE BUY: %s/%s | $%.2f NO @ $%.3f (%.2f shares) token=%s",
                     city_slug, date_str, bet_size, no_price, shares, token_id)

        order_resp = trader.buy_no_tokens(
            token_id=token_id,
            price=no_price,
            size=shares,
            neg_risk=neg_risk,
        )

        if order_resp:
            pos = trader.record_entry(
                state=state,
                city_slug=city_slug,
                date_str=date_str,
                bet_size=bet_size,
                entry_no_price=no_price,
                market_id=signal["market_id"],
                token_id=token_id,
                question=signal["question"],
                bucket_range=signal["bucket_range"],
                weather_com_high=signal["wc_high"],
                open_meteo_high=signal["om_high"],
                distance=signal["distance"],
                order_resp=order_resp,
            )
            opened += 1
            logger.info("LIVE OPENED (rank #%d): %s %s -- $%.2f NO @ $%.3f (dist=%.1f)",
                        all_signals.index(signal) + 1,
                        city_slug, date_str,
                        bet_size, no_price, signal["distance"])
        else:
            logger.error("Order failed for %s/%s", city_slug, date_str)

    return opened


# ── Position Monitoring ──────────────────────────────────────────────────

def monitor_all_positions(stations: dict, state: dict) -> int:
    """
    Monitor all open positions. Returns number of positions closed.
    """
    closed = 0
    keys_to_check = list(state["open_positions"].keys())

    for key in keys_to_check:
        pos = state["open_positions"].get(key)
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
                    # Market resolved YES — we lose
                    token_id = pos.get("token_id", "")
                    order_resp = None
                    if token_id:
                        order_resp = trader.sell_no_market(token_id, pos.get("shares", 0))
                    trader.record_exit(state, city_slug, date_str, "resolution_loss")
                elif price_data["yes_price"] <= 0.05:
                    # Market resolved NO — we win
                    trader.record_exit(state, city_slug, date_str, "resolution_win")
                else:
                    trader.record_exit(state, city_slug, date_str, "resolution_loss")
                closed += 1
                continue
        else:
            current_no = pos["entry_no_price"]
            pos["current_no_price"] = current_no
            pos["last_monitored"] = datetime.now(timezone.utc).isoformat()

        # Run monitoring strategy
        action = monitor_position(city_slug, station, date_str, pos, None,
                                   current_no_price=current_no)

        if action == "sell":
            token_id = pos.get("token_id", "")
            shares = pos.get("shares", 0)
            order_resp = None
            if token_id and shares > 0:
                order_resp = trader.sell_no_market(token_id, shares)
            trader.record_exit(state, city_slug, date_str, "monitor_sell", current_no)
            closed += 1
        elif action == "tighten":
            logger.info("Tightening monitoring for %s", key)

    # Persist updated prices
    if keys_to_check:
        trader.save_state(state)

    return closed


# ── Main Commands ─────────────────────────────────────────────────────────

def cmd_scan():
    """One-shot: discover markets, evaluate entries, print results."""
    stations = load_stations()
    state = trader.load_state()

    IST = timezone(timedelta(hours=5, minutes=30))
    today_ist = datetime.now(IST).strftime("%Y-%m-%d")
    print(f"\n{'='*55}")
    print(f"  WEATHER NO TRADER — SCAN (LIVE)")
    print(f"{'='*55}")
    print(f"  Balance:   ${state['balance']:.2f}")
    print(f"  Open:      {len(state['open_positions'])}")
    print(f"  IST date:  {today_ist}")
    print(f"  Scanning {len(stations)} cities...\n")

    markets = discover_markets(stations)
    if not markets:
        print("  No markets found.")
        return

    print(f"\n  Found {len(markets)} markets. Evaluating entries...\n")
    opened = scan_entries(stations, markets, state)

    print(f"\n  Scan complete: {opened} new position(s) opened")
    print(f"  Balance: ${state['balance']:.2f}")


def cmd_run():
    """Continuous loop: scan + monitor."""
    stations = load_stations()
    state = trader.load_state()

    print(f"\n{'='*55}")
    print(f"  WEATHER NO TRADER — RUNNING (LIVE)")
    print(f"{'='*55}")
    print(f"  Balance:      ${state['balance']:.2f}")
    print(f"  Open:         {len(state['open_positions'])}")
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
                opened = scan_entries(stations, markets, state)
                last_full_scan = time.time()
                last_weather_poll = time.time()
                print(f"  balance: ${state['balance']:.2f} | open: {len(state['open_positions'])} | new: {opened}")

            # Monitor positions every METAR_POLL_SECONDS
            elif now_ts - last_weather_poll >= METAR_POLL_SECONDS:
                if len(state['open_positions']) > 0:
                    print(f"[{now_str}] monitoring {len(state['open_positions'])} position(s)...")
                    closed = monitor_all_positions(stations, state)
                    last_weather_poll = time.time()
                    print(f"  balance: ${state['balance']:.2f} | closed: {closed}")

        except KeyboardInterrupt:
            print(f"\n  Stopping — saving state...")
            trader.save_state(state)
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
    state = trader.load_state()
    balance = state.get("balance", 5.0)
    starting = state.get("starting_bankroll", 5.0)
    open_pos = state.get("open_positions", {})
    closed = state.get("closed_positions", [])

    print(f"\n{'='*55}")
    print(f"  WEATHER NO TRADER — STATUS (LIVE)")
    print(f"{'='*55}")
    print(f"  Balance:    ${balance:.2f} (start ${starting:.2f})")
    print(f"  Open:       {len(open_pos)}")
    print(f"  Closed:     {len(closed)}")

    if open_pos:
        print(f"\n  Open positions:")
        for key, pos in open_pos.items():
            bucket = f"{pos['bucket_low']}-{pos['bucket_high']}"
            token = pos.get("token_id", "")[:16] + "..."
            print(f"    {key}: $%.2f NO @ $%.3f | bucket %s | token %s" %
                  (pos["bet_size"], pos["entry_no_price"], bucket, token))
    print()


def cmd_report():
    """Full report of closed positions."""
    state = trader.load_state()
    closed = state.get("closed_positions", [])

    if not closed:
        print("  No closed positions yet.")
        return

    print(f"\n{'='*70}")
    print(f"  WEATHER NO TRADER — TRADE REPORT (LIVE)")
    print(f"{'='*70}")

    for pos in sorted(closed, key=lambda x: x.get("closed_at", "")):
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

    total = len(closed)
    wins = sum(1 for p in closed if (p.get("pnl", 0) or 0) >= 0)
    total_pnl = sum(p.get("pnl", 0) or 0 for p in closed)
    print(f"  W/L: {wins}W / {total - wins}L | P/L: {'+'if total_pnl>=0 else ''}${total_pnl:.2f}")
    print(f"  Balance: ${state.get('balance', 5):.2f}")
    print()


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
