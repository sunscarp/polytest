#!/usr/bin/env python3
"""
Weather NO Recommender — Main Orchestrator

Discovers Polymarket temperature markets, recommends BUYs (dashboard only),
and monitors REAL on-chain positions to recommend SELLS (via email).

In PAPER_TRADING mode, automatically opens/closes virtual positions using
the same strategy logic, with no real trades and no emails.

Usage:
    python main.py              # run strategy + monitoring loop
    python main.py scan         # one-shot scan (no monitoring loop)
    python main.py status       # show current state
    python main.py report       # full report of sell recommendations
"""

import sys
import json
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import (
    METAR_POLL_SECONDS, WEATHER_POLL_SECONDS,
    MIN_VOLUME, LOGS_DIR,
    PAPER_TRADING, MAX_OPEN_PAPER, MAX_BET,
)
import trader
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

# ── Simulator singleton (created fresh each time, loads persisted state) ──

sim = Simulator()


# ── Station Loading ───────────────────────────────────────────────────────

def load_stations() -> dict:
    from config import STATIONS_FILE
    return json.loads(STATIONS_FILE.read_text(encoding="utf-8"))


# ── Market Discovery ──────────────────────────────────────────────────────

def get_allowed_regions() -> set:
    IST = timezone(timedelta(hours=5, minutes=30))
    hour = datetime.now(IST).hour
    if hour < 8:
        return {"asia"}
    elif hour < 15:
        return {"asia", "europe", "africa"}
    else:
        return {"asia", "europe", "africa", "americas"}


def discover_markets(stations: dict, skip_cities: set = None, target_date: str = None) -> list[dict]:
    """
    Find available Polymarket temperature markets for a given date.
    Returns list of {city_slug, station, date_str, event_slug, buckets} dicts.
    """
    IST = timezone(timedelta(hours=5, minutes=30))
    if target_date is None:
        target_date = datetime.now(IST).strftime("%Y-%m-%d")
    markets = []
    if skip_cities is None:
        skip_cities = set()

    allowed_regions = get_allowed_regions()
    logger.info("IST date: %s | allowed regions: %s", target_date, allowed_regions)

    for city_slug, station in stations.items():
        if city_slug in skip_cities:
            continue

        region = station.get("region", "asia")
        if region not in allowed_regions:
            continue

        event = polymarket.get_event(city_slug, target_date)
        if event and event.get("markets"):
            has_volume = any(
                float(m.get("volume", 0)) >= MIN_VOLUME
                for m in event.get("markets", [])
            )
            if has_volume:
                buckets = polymarket.get_city_buckets(city_slug, target_date)
                markets.append({
                    "city_slug": city_slug,
                    "station": station,
                    "date_str": target_date,
                    "event_slug": event.get("slug", ""),
                    "market_count": len(event.get("markets", [])),
                    "buckets": buckets,
                })
                logger.info("Found market: %s %s (%d buckets)",
                            station["name"], target_date, len(buckets))
        else:
            skip_cities.add(city_slug)
            logger.info("No data for %s %s — skipping", station["name"], target_date)

        time.sleep(0.3)

    return markets


# ── Buy Recommendations (Dashboard Only, No Email) ───────────────────────

def scan_entries(stations: dict, markets: list[dict]) -> int:
    """
    Evaluate entry signals for all discovered markets.
    Logs buy recommendations for the dashboard — NO email sent.
    In PAPER_TRADING mode, auto-opens positions via Simulator.
    Returns number of signals found.
    """
    all_signals = []
    for market in markets:
        city_slug = market["city_slug"]
        station = market["station"]
        date_str = market["date_str"]

        signal = evaluate_entry(city_slug, station, date_str)
        if signal is not None:
            all_signals.append(signal)

    if not all_signals:
        return 0

    all_signals.sort(key=lambda s: s["distance"], reverse=True)

    for signal in all_signals:
        trader._log_buy_recommendation(signal)
        logger.info("BUY REC: %s/%s | $%.2f NO @ $%.3f (dist=%.1f)",
                     signal["city_slug"], signal["date"],
                     signal["bet_size"], signal["no_price"], signal["distance"])

    # Paper trading: auto-buy signals until we hit the limit
    if PAPER_TRADING:
        for signal in all_signals:
            if sim.open_count() >= MAX_OPEN_PAPER:
                break
            city_slug = signal["city_slug"]
            date_str = signal["date"]

            if not sim.has_position(city_slug, date_str):
                pos = sim.open_position(
                    city_slug=city_slug,
                    date_str=date_str,
                    bet_size=signal["bet_size"],
                    entry_no_price=signal["no_price"],
                    market_id=signal["market_id"],
                    question=signal["question"],
                    bucket_range=signal["bucket_range"],
                    weather_com_high=signal["wc_high"],
                    open_meteo_high=signal.get("om_high"),
                    distance=signal["distance"],
                )
                if pos:
                    logger.info("[PAPER] OPENED: %s/%s $%.2f NO @ $%.3f (dist=%.1f, wc_high=%.1f)",
                                city_slug, date_str, signal["bet_size"], signal["no_price"],
                                signal["distance"], signal["wc_high"])

    return len(all_signals)


# ── Monitor Paper Positions ──────────────────────────────────────────────

def monitor_paper_positions(stations: dict, weather_markets: list[dict]) -> int:
    """
    Monitor open paper positions with the same strategy as real positions.
    Auto-closes on sell signals. No email sent (PAPER_TRADING guard in trader).
    Returns number of paper positions closed.
    """
    if not PAPER_TRADING or sim.open_count() == 0:
        return 0

    # Build market lookup for price data
    token_lookup = {}
    for wm in weather_markets:
        for bucket in wm.get("buckets", []):
            tid = bucket.get("token_id", "")
            if tid:
                token_lookup[tid] = {
                    "city_slug": wm["city_slug"],
                    "date_str": wm["date_str"],
                    "station": wm["station"],
                    "market_id": bucket["market_id"],
                }

    # Also build city_slug+date -> station mapping
    city_station_map = {}
    for wm in weather_markets:
        city_station_map[f"{wm['city_slug']}_{wm['date_str']}"] = wm["station"]

    closes = 0
    mon_state = trader.load_monitoring_state()

    for key, pos in list(sim.open_positions.items()):
        city_slug = pos["city_slug"]
        date_str = pos["date"]
        station = stations.get(city_slug)
        if not station:
            # Try from weather markets
            station = city_station_map.get(key)
        if not station:
            logger.warning("[PAPER] No station for %s, skipping monitor", key)
            continue

        # Get current market price
        current_no = pos["entry_no_price"]  # fallback
        from data_sources import polymarket as pm
        price_data = pm.get_market_price(pos["market_id"])
        if price_data:
            current_no = price_data["no_price"]

            # Check if market resolved
            if price_data.get("closed"):
                if price_data["yes_price"] >= 0.95:
                    sim.close_position(city_slug, date_str, "resolution_loss", current_no)
                elif price_data["yes_price"] <= 0.05:
                    sim.close_position(city_slug, date_str, "resolution_win", current_no)
                else:
                    sim.close_position(city_slug, date_str, "resolution_loss", current_no)
                closes += 1
                logger.info("[PAPER] RESOLVED: %s", key)
                continue

        # Load monitoring history
        pos_mon = mon_state.get(key, {})

        # Build position dict compatible with monitor_position()
        pos_dict = {
            "city_slug": city_slug,
            "date": date_str,
            "market_id": pos["market_id"],
            "bucket_low": pos["bucket_low"],
            "bucket_high": pos["bucket_high"],
            "entry_no_price": pos["entry_no_price"],
            "bet_size": pos["bet_size"],
            "current_no_price": current_no,
            "monitoring_events": pos.get("monitoring_events", []),
            "metar_distances": pos_mon.get("metar_distances", []),
            "last_metar_temp": pos_mon.get("last_metar_temp"),
            "last_wc_current": pos_mon.get("last_wc_current"),
            "last_wc_high": pos_mon.get("last_wc_high"),
        }

        # Run monitoring strategy
        action = monitor_position(city_slug, station, date_str, pos_dict, None,
                                   current_no_price=current_no)

        # Save monitoring events back to simulator position
        pos["monitoring_events"] = pos_dict.get("monitoring_events", [])

        # Save monitoring state
        mon_state[key] = {
            "metar_distances": pos_dict.get("metar_distances", []),
            "last_metar_temp": pos_dict.get("last_metar_temp"),
            "last_wc_current": pos_dict.get("last_wc_current"),
            "last_wc_high": pos_dict.get("last_wc_high"),
            "monitoring_events": pos_dict.get("monitoring_events", [])[-20:],
        }

        if action == "sell":
            # Extract reason from last monitoring event
            events = pos_dict.get("monitoring_events", [])
            reason = "monitor_sell"
            if events:
                last_event = events[-1]
                action_type = last_event.get("action", "")
                if action_type == "sell_forecast_conflict":
                    reason = "forecast_conflict"
                elif action_type == "sell_critical":
                    reason = "critical_close"
                elif action_type == "sell_stop_loss":
                    reason = "stop_loss"
                elif action_type == "sell_take_profit":
                    reason = "take_profit"

            closed = sim.close_position(city_slug, date_str, reason, current_no)
            if closed:
                closes += 1
                mon_state.pop(key, None)
                logger.info("[PAPER] CLOSED: %s reason=%s pnl=$%.2f balance=$%.2f",
                            key, reason, closed.get("pnl", 0), sim.balance)

        elif action == "forecast_drift":
            events = pos_dict.get("monitoring_events", [])
            if events:
                last_event = events[-1]
                old_high = last_event.get("prev_wc_high", 0)
                new_high = last_event.get("wc_high", 0)
                if old_high and new_high:
                    # Log but don't close — informational for paper mode
                    logger.info("[PAPER] FORECAST DRIFT: %s (%.1f -> %.1f)",
                                key, old_high, new_high)
                    # Still send email (trader.py guards it in PAPER mode)
                    trader.notify_forecast_drift(pos, old_high, new_high, current_no)

        elif action == "tighten":
            logger.info("[PAPER] TIGHTENING: %s", key)

        else:
            logger.debug("[PAPER] HOLD: %s (action=%s)", key, action)

    trader.save_monitoring_state(mon_state)
    sim.save_state()
    return closes


# ── Sell Recommendations (Real Positions, Email) ─────────────────────────

def monitor_real_positions(stations: dict, weather_markets: list[dict]) -> int:
    """
    Fetch real on-chain positions, match to weather markets,
    monitor with strategy, and recommend SELLS via email.
    Returns number of sell recommendations sent.
    """
    positions = trader.get_positions()
    if not positions:
        return 0

    matched = trader.match_positions_to_markets(positions, weather_markets)
    if not matched:
        return 0

    logger.info("Monitoring %d real weather position(s)", len(matched))
    mon_state = trader.load_monitoring_state()
    sells_sent = 0

    for mpos in matched:
        city_slug = mpos["city_slug"]
        date_str = mpos["date_str"]
        key = f"{city_slug}_{date_str}"

        station = mpos["station"]
        if not station:
            continue

        # Load monitoring history for this position
        pos_mon = mon_state.get(key, {})

        # Get current market price
        from data_sources import polymarket as pm
        price_data = pm.get_market_price(mpos["market_id"])
        if price_data:
            current_no = price_data["no_price"]
            mpos["current_no_price"] = current_no

            # Check if market resolved
            if price_data.get("closed"):
                if price_data["yes_price"] >= 0.95:
                    trader.notify_sell(mpos, "resolution_loss", current_no)
                    sells_sent += 1
                elif price_data["yes_price"] <= 0.05:
                    trader.notify_sell(mpos, "resolution_win", current_no)
                    sells_sent += 1
                else:
                    trader.notify_sell(mpos, "resolution_loss", current_no)
                    sells_sent += 1
                continue
        else:
            current_no = mpos["last_price"] if mpos["last_price"] > 0 else mpos["entry_no_price"]
            mpos["current_no_price"] = current_no

        # Build position dict compatible with monitor_position()
        pos_dict = {
            "city_slug": city_slug,
            "date": date_str,
            "market_id": mpos["market_id"],
            "bucket_low": mpos["bucket_low"],
            "bucket_high": mpos["bucket_high"],
            "entry_no_price": mpos["entry_no_price"],
            "bet_size": mpos["bet_size"],
            "current_no_price": current_no,
            "monitoring_events": pos_mon.get("monitoring_events", []),
            "metar_distances": pos_mon.get("metar_distances", []),
            "last_metar_temp": pos_mon.get("last_metar_temp"),
            "last_wc_current": pos_mon.get("last_wc_current"),
            "last_wc_high": pos_mon.get("last_wc_high"),
        }

        # Run monitoring strategy
        action = monitor_position(city_slug, station, date_str, pos_dict, None,
                                   current_no_price=current_no)

        # Save monitoring state back
        mon_state[key] = {
            "metar_distances": pos_dict.get("metar_distances", []),
            "last_metar_temp": pos_dict.get("last_metar_temp"),
            "last_wc_current": pos_dict.get("last_wc_current"),
            "last_wc_high": pos_dict.get("last_wc_high"),
            "monitoring_events": pos_dict.get("monitoring_events", []),
        }

        if action == "forecast_drift":
            # Get drift details from the last monitoring event
            events = pos_dict.get("monitoring_events", [])
            if events:
                last_event = events[-1]
                old_high = last_event.get("prev_wc_high", 0)
                new_high = last_event.get("wc_high", 0)
                if old_high and new_high:
                    trader.notify_forecast_drift(mpos, old_high, new_high, current_no)
                    logger.info("FORECAST DRIFT alert sent: %s/%s (%.1f -> %.1f)",
                                city_slug, date_str, old_high, new_high)
        elif action == "sell":
            # Get reason from the last monitoring event
            events = pos_dict.get("monitoring_events", [])
            reason = "monitor_sell"
            if events:
                last_event = events[-1]
                action_type = last_event.get("action", "")
                if action_type == "sell_forecast_conflict":
                    reason = "forecast_conflict"
                elif action_type == "sell_critical":
                    reason = "critical_close"
                elif action_type == "sell_stop_loss":
                    reason = "stop_loss"
                elif action_type == "sell_take_profit":
                    reason = "take_profit"
            trader.notify_sell(mpos, reason, current_no)
            sells_sent += 1
            # Remove from monitoring after sell rec
            mon_state.pop(key, None)
            logger.info("SELL REC sent: %s/%s (reason=%s)", city_slug, date_str, reason)
        elif action == "tighten":
            logger.info("Tightening monitoring for %s", key)
        else:
            logger.info("HOLD: %s/%s (action=%s)", city_slug, date_str, action)

    trader.save_monitoring_state(mon_state)
    return sells_sent


# ── Summary API (for cron health checks) ─────────────────────────────────

def get_summary() -> dict:
    """Return a machine-readable summary for cron / health checks."""
    paper_summary = sim.summary() if PAPER_TRADING else None
    buy_recs = trader.load_buy_recommendations()
    sell_recs = trader.load_sell_recommendations()
    positions = trader.get_enriched_positions()

    now = datetime.now(timezone.utc).isoformat()

    result = {
        "ts": now,
        "mode": "paper" if PAPER_TRADING else "recommendation",
        "positions_real": len(positions),
        "buy_recs": len(buy_recs),
        "sell_recs": len(sell_recs),
        "emails_sent": sum(1 for r in sell_recs if r.get("email_sent")),
    }

    if paper_summary:
        result["paper"] = paper_summary

    return result


# ── Main Commands ─────────────────────────────────────────────────────────

def cmd_scan():
    stations = load_stations()
    IST = timezone(timedelta(hours=5, minutes=30))
    today_ist = datetime.now(IST).strftime("%Y-%m-%d")
    yesterday_ist = (datetime.now(IST) - timedelta(days=1)).strftime("%Y-%m-%d")

    mode_str = "PAPER TRADING" if PAPER_TRADING else "RECOMMENDATION"
    print(f"\n{'='*55}")
    print(f"  WEATHER NO RECOMMENDER — SCAN ({mode_str})")
    print(f"{'='*55}")
    print(f"  Scanning {len(stations)} cities...\n")

    # Scan today + yesterday for market cache
    all_markets = []
    for d in [yesterday_ist, today_ist]:
        markets = discover_markets(stations, target_date=d)
        all_markets.extend(markets)
    trader.save_market_cache(all_markets)

    if not all_markets:
        print("  No markets found.")
        return

    today_markets = [m for m in all_markets if m["date_str"] == today_ist]
    print(f"\n  Found {len(all_markets)} markets ({len(today_markets)} today). Evaluating entries...\n")
    recs = scan_entries(stations, today_markets)
    print(f"\n  {recs} buy recommendation(s) logged")

    if PAPER_TRADING:
        print(f"  Paper balance: ${sim.balance:.2f} | Open: {sim.open_count()}")

    sells = monitor_real_positions(stations, all_markets)
    print(f"  {sells} sell recommendation(s) sent (real)")

    if PAPER_TRADING:
        paper_closes = monitor_paper_positions(stations, all_markets)
        print(f"  {paper_closes} paper position(s) closed")
        sim.print_summary()


def cmd_run():
    stations = load_stations()
    mode_str = "PAPER TRADING" if PAPER_TRADING else "RECOMMENDATION"

    print(f"\n{'='*55}")
    print(f"  WEATHER NO RECOMMENDER — RUNNING ({mode_str})")
    print(f"{'='*55}")
    print(f"  Mode:       {mode_str}")
    if PAPER_TRADING:
        print(f"  Bankroll:   ${sim.balance:.2f} (start ${sim.starting_bankroll:.2f})")
        print(f"  Max open:   {MAX_OPEN_PAPER}")
    print(f"  METAR poll: every {METAR_POLL_SECONDS}s")
    print(f"  Weather poll: every {WEATHER_POLL_SECONDS}s")
    print(f"  Ctrl+C to stop\n")

    last_full_scan = 0
    last_weather_poll = 0
    skip_cities = set()
    IST = timezone(timedelta(hours=5, minutes=30))
    current_ist_date = datetime.now(IST).strftime("%Y-%m-%d")
    cached_markets = []

    while True:
        now_ts = time.time()
        ist_now = datetime.now(IST)
        now_str = ist_now.strftime("%H:%M:%S IST")

        try:
            # Full market scan every WEATHER_POLL_SECONDS
            if now_ts - last_full_scan >= WEATHER_POLL_SECONDS:
                today_ist = datetime.now(IST).strftime("%Y-%m-%d")
                yesterday_ist = (datetime.now(IST) - timedelta(days=1)).strftime("%Y-%m-%d")
                if today_ist != current_ist_date:
                    skip_cities = set()
                    current_ist_date = today_ist

                print(f"[{now_str}] scanning {yesterday_ist} + {today_ist}...")
                all_markets = []
                for d in [yesterday_ist, today_ist]:
                    all_markets.extend(discover_markets(stations, skip_cities, target_date=d))
                trader.save_market_cache(all_markets)
                cached_markets = all_markets

                today_markets = [m for m in all_markets if m["date_str"] == today_ist]
                buy_recs = scan_entries(stations, today_markets)
                last_full_scan = time.time()
                last_weather_poll = time.time()
                if PAPER_TRADING:
                    print(f"  {len(all_markets)} markets | buy recs: {buy_recs} | "
                          f"paper: ${sim.balance:.2f} ({sim.open_count()} open)")
                else:
                    print(f"  {len(all_markets)} markets | buy recs: {buy_recs}")

            # Monitor real positions EVERY iteration
            if cached_markets:
                sells = monitor_real_positions(stations, cached_markets)
                if sells:
                    print(f"  *** SELL REC SENT: {sells} ***")

                # Monitor paper positions EVERY iteration
                if PAPER_TRADING:
                    paper_closes = monitor_paper_positions(stations, cached_markets)
                    if paper_closes:
                        print(f"  [PAPER] Closed {paper_closes} position(s) | "
                              f"balance: ${sim.balance:.2f}")

        except KeyboardInterrupt:
            print(f"\n  Stopping...")
            if PAPER_TRADING:
                sim.print_summary()
            break
        except Exception as e:
            logger.error("Error in main loop: %s", e)
            time.sleep(30)
            continue

        # Write timer state for dashboard
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

        time.sleep(METAR_POLL_SECONDS)


def cmd_status():
    mode_str = "PAPER TRADING" if PAPER_TRADING else "RECOMMENDATION"
    print(f"\n{'='*55}")
    print(f"  WEATHER NO RECOMMENDER — STATUS ({mode_str})")
    print(f"{'='*55}")

    # Real positions
    positions = trader.get_positions()
    weather_pos = [p for p in positions if p.get("outcome", "").lower() == "no"]
    print(f"  Positions: {len(weather_pos)} NO weather positions (of {len(positions)} total)")

    # Paper positions
    if PAPER_TRADING:
        print(f"  Paper:     {sim.open_count()} open | balance: ${sim.balance:.2f}")
        for key, pos in sim.open_positions.items():
            print(f"    - {key}: $%.2f NO @ $%.3f (dist=%.1f)" %
                  (pos["bet_size"], pos["entry_no_price"], pos["distance"]))

    # Recent recommendations
    buy_recs = trader.load_buy_recommendations()
    sell_recs = trader.load_sell_recommendations()
    print(f"  Buy recs:  {len(buy_recs)}")
    print(f"  Sell recs: {len(sell_recs)}")
    print()


def cmd_report():
    sell_recs = trader.load_sell_recommendations()

    mode_str = "PAPER TRADING" if PAPER_TRADING else "RECOMMENDATION"
    print(f"\n{'='*70}")
    print(f"  WEATHER NO RECOMMENDER — REPORT ({mode_str})")
    print(f"{'='*70}")

    if PAPER_TRADING:
        sim.print_summary()

    if sell_recs:
        print(f"\n  Sell Recommendations:")
        for rec in sorted(sell_recs, key=lambda x: x.get("ts", "")):
            city = rec.get("city_slug", "?").upper()
            bucket = f"{rec.get('bucket_low', '?')}-{rec.get('bucket_high', '?')}"
            pnl = rec.get("pnl", 0)
            reason = rec.get("reason", "?")
            email = "SENT" if rec.get("email_sent") else "SUPPRESSED"
            ts = rec.get("ts", "")[:19]

            print(f"  {ts}  {city:<12} {bucket:<10} "
                  f"P/L: {'+'if pnl>=0 else ''}${pnl:.2f} "
                  f"[{reason}] email={email}")

        total = len(sell_recs)
        wins = sum(1 for r in sell_recs if (r.get("pnl", 0) or 0) >= 0)
        total_pnl = sum(r.get("pnl", 0) or 0 for r in sell_recs)
        print(f"\n  W/L: {wins}W / {total - wins}L | P/L: {'+'if total_pnl>=0 else ''}${total_pnl:.2f}")

    if PAPER_TRADING and sim.closed_positions:
        print(f"\n  Paper Closed Positions:")
        for pos in sim.closed_positions:
            city = pos["city_slug"].upper()
            bucket = f"{pos['bucket_low']:.0f}-{pos['bucket_high']:.0f}"
            pnl = pos.get("pnl", 0)
            reason = pos.get("exit_reason", "?")
            print(f"  {pos.get('opened_at','')[:19]}  {city:<12} {bucket:<10} "
                  f"P/L: {'+'if pnl>=0 else ''}${pnl:.2f} [{reason}]")

    print(f"\n{'='*70}\n")


def cmd_summary():
    """Print JSON summary for cron/health checks."""
    summary = get_summary()
    print(_json.dumps(summary, indent=2))


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
    elif cmd == "summary":
        cmd_summary()
    else:
        print("Usage: python main.py [run|scan|status|report|summary]")
