"""
Strategy logic for the NO trading simulator.

Entry: Buy "NO" when weather.com forecast high is 2-4°C from a bucket threshold,
       Open-Meteo agrees directionally, and the NO position is cheap.

Monitoring: Poll weather.com (10 min) and METAR (3 min) to detect temperature
            shifts. Exit based on 4-case state machine.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from config import (
    DISTANCE_MIN, DISTANCE_MAX, NOISE_THRESHOLD, MIN_NO_PRICE,
    MAX_NO_PRICE, MIN_VOLUME, STOP_LOSS_PCT,
    PROXIMITY_THRESHOLD, METAR_CLOSE_READINGS, FORECAST_DRIFT_THRESHOLD,
)
from data_sources import weather_com, open_meteo, aviation_weather, polymarket

logger = logging.getLogger(__name__)


def _record_event(position: dict, event: dict, sim=None):
    """Record a monitoring event — via simulator or directly to position."""
    if sim is not None:
        # Will be handled by simulator
        pass
    else:
        position.setdefault("monitoring_events", []).append(event)


# ── Entry Logic ───────────────────────────────────────────────────────────

def evaluate_entry(city_slug: str, station: dict, date_str: str) -> Optional[dict]:
    """
    Evaluate whether to enter a NO position for a city/date.

    Args:
        city_slug: e.g. "nyc"
        station: station dict from stations.json
        date_str: "YYYY-MM-DD"

    Returns:
        Entry signal dict or None if no trade.
    """
    unit = station["unit"]
    lat, lon = station["lat"], station["lon"]
    icao = station["icao"]
    tz = station["timezone"]

    temp_unit_wc = "e" if unit == "F" else "m"       # weather.com unit code
    temp_unit_om = "fahrenheit" if unit == "F" else "celsius"

    # 1. Get weather.com daily high
    wc_high = weather_com.get_daily_high(lat, lon, date_str, temp_unit_wc)
    if wc_high is None:
        logger.info("[%s/%s] weather.com: no data, skipping", city_slug, date_str)
        return None

    # 2. Get Polymarket buckets
    buckets = polymarket.get_city_buckets(city_slug, date_str)
    if not buckets:
        logger.info("[%s/%s] Polymarket: no markets found, skipping", city_slug, date_str)
        return None

    # 3. For each bucket, check entry conditions
    best_signal = None
    best_distance = 0

    for bucket in buckets:
        if bucket["closed"] or not bucket["active"]:
            continue

        t_low, t_high = bucket["range"]

        # Calculate the threshold midpoint for distance measurement
        # For edge buckets ("X or below" / "X or higher"), use the boundary value
        if t_low == -999:
            threshold = t_high  # "X or below" — distance from X
        elif t_high == 999:
            threshold = t_low   # "X or higher" — distance from X
        else:
            # Regular bucket — use the nearest boundary
            threshold = t_high if wc_high > (t_low + t_high) / 2 else t_low

        distance = abs(wc_high - threshold)

        # Entry condition 1: distance in 2-4°C band
        if distance < DISTANCE_MIN or distance > DISTANCE_MAX:
            continue

        # Entry condition 2: NO must be cheap (YES price < threshold)
        # We're buying NO, so we want YES to be underpriced relative to reality
        if bucket["yes_price"] >= MIN_NO_PRICE:
            continue

        # Entry condition 2b: NO price cap — skip if NO > 0.90 (low upside)
        if bucket["no_price"] > MAX_NO_PRICE:
            continue

        # Entry condition 3: minimum volume
        if bucket["volume"] < MIN_VOLUME:
            continue

        # Entry condition 4: Open-Meteo cross-check
        om_result = open_meteo.get_forecast_direction(
            lat, lon, date_str, threshold, temp_unit_om, tz
        )
        if om_result is None:
            logger.info("[%s/%s] Open-Meteo: no data, skipping bucket %.0f-%.0f",
                        city_slug, date_str, t_low, t_high)
            continue

        # Open-Meteo should agree that the threshold is unlikely to hit
        # i.e. its forecast high should also be away from the threshold
        om_distance = om_result["distance"]
        if om_distance < DISTANCE_MIN * 0.5:
            # Open-Meteo thinks the threshold is close — disagree, skip
            logger.info("[%s/%s] Open-Meteo disagrees: high=%.1f near threshold %.1f (dist=%.1f)",
                        city_slug, date_str, om_result["high"], threshold, om_distance)
            continue

        # Pick the best signal (largest distance = most confident NO)
        if distance > best_distance:
            best_distance = distance
            best_signal = {
                "city_slug": city_slug,
                "date": date_str,
                "market_id": bucket["market_id"],
                "token_id": bucket.get("token_id", ""),
                "neg_risk": bucket.get("neg_risk", False),
                "question": bucket["question"],
                "bucket_range": (t_low, t_high),
                "threshold": threshold,
                "yes_price": bucket["yes_price"],
                "no_price": bucket["no_price"],
                "volume": bucket["volume"],
                "wc_high": wc_high,
                "om_high": om_result["high"],
                "distance": distance,
            }

    if best_signal is None:
        logger.info("[%s/%s] No entry signal found", city_slug, date_str)
        return None

    # Fixed $1 bet for real testing
    best_signal["bet_size"] = 1.0

    logger.info("ENTRY SIGNAL %s/%s: high=%.1f threshold=%.1f dist=%.1f bet=$%.2f NO@$%.3f",
                city_slug, date_str, wc_high, best_signal["threshold"],
                best_signal["distance"], best_signal["bet_size"], best_signal["no_price"])

    return best_signal


# ── Monitoring State Machine ──────────────────────────────────────────────

def monitor_position(city_slug: str, station: dict, date_str: str,
                     position: dict, sim=None,
                     current_no_price: Optional[float] = None) -> Optional[str]:
    """
    Monitor an open position and decide action.

    Strategy:
    1. Re-fetch weather.com DAILY HIGH each cycle — if it now covers the
       bucket, sell immediately (forecast has shifted against us).
    2. Compare METAR current temp vs bucket: within 1°C and weather.com
       confirms → sell immediately.
    3. Track METAR trend for "closing in" detection.
    4. Existing 4-case state machine for trend-based exits.

    Args:
        current_no_price: live NO price from Polymarket (for P/L calc).

    Returns:
        Action string: "hold", "sell", "tighten", or "wait_for_stop"
    """
    unit = station["unit"]
    lat, lon = station["lat"], station["lon"]
    icao = station["icao"]

    temp_unit_wc = "e" if unit == "F" else "m"

    # 1. Get weather.com daily high (re-fetched every cycle!)
    wc_high = weather_com.get_daily_high(lat, lon, date_str, temp_unit_wc)

    # 1b. FORECAST DRIFT CHECK — alert if daily high shifted toward the bucket
    prev_wc_high = position.get("last_wc_high")
    if prev_wc_high is not None and wc_high is not None and prev_wc_high != wc_high:
        shift = wc_high - prev_wc_high
        bucket_low = position["bucket_low"]
        bucket_high = position["bucket_high"]

        # Determine if shift moved toward the bucket (dangerous for NO)
        toward_bucket = False
        if bucket_high == 999:
            # "X or higher" — higher forecast = more dangerous
            toward_bucket = shift > 0
        elif bucket_low == -999:
            # "X or below" — lower forecast = more dangerous
            toward_bucket = shift < 0
        else:
            # Exact bucket — shift toward bucket_mid = dangerous
            bucket_mid = (bucket_low + bucket_high) / 2
            old_dist = abs(prev_wc_high - bucket_mid)
            new_dist = abs(wc_high - bucket_mid)
            toward_bucket = new_dist < old_dist

        if toward_bucket and abs(shift) >= FORECAST_DRIFT_THRESHOLD:
            # Get current P/L
            if current_no_price is not None and current_no_price > 0:
                current_no = current_no_price
            else:
                current_no = position.get("entry_no_price", 0)
            entry_no = position.get("entry_no_price", 0)
            pnl_pct = ((current_no / entry_no - 1.0) * 100) if entry_no > 0 else 0

            event = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "wc_high": wc_high,
                "prev_wc_high": prev_wc_high,
                "shift": round(shift, 1),
                "metar_temp": None,
                "distance_to_threshold": None,
                "closing_in": False,
                "running_cool": False,
                "in_profit": pnl_pct >= 0,
                "pnl_pct": round(pnl_pct, 1),
                "action": "forecast_drift",
                "reason": f"wc_high {prev_wc_high:.1f} -> {wc_high:.1f} ({shift:+.1f})",
            }
            _record_event(position, event, sim)
            logger.info("[%s/%s] FORECAST DRIFT: wc_high %.1f -> %.1f (%+.1f), P/L %.1f%%",
                        city_slug, date_str, prev_wc_high, wc_high, shift, pnl_pct)
            return "forecast_drift"

    # 2. Get weather.com current-hour forecast
    wc_current = weather_com.get_current_hour_temp(lat, lon, temp_unit_wc)
    if wc_current is None:
        logger.warning("[%s/%s] Monitoring: weather.com unavailable, holding", city_slug, date_str)
        return "hold"

    # 3. Get METAR current observation
    metar = aviation_weather.get_current_temp(icao)
    if metar is None:
        logger.warning("[%s/%s] Monitoring: METAR unavailable, holding", city_slug, date_str)
        return "hold"

    metar_temp_c = metar["temp_c"]
    if unit == "F":
        metar_temp = metar["temp_f"]
    else:
        metar_temp = metar_temp_c

    # Save latest readings so dashboard always has data
    position["last_metar_temp"] = metar_temp
    position["last_wc_current"] = wc_current
    position["last_wc_high"] = wc_high

    # 4. Compute diff between METAR and current-hour forecast
    diff = abs(wc_current - metar_temp)

    # 5. $0.99 sell — fire regardless of proximity or trend
    if current_no_price is not None and current_no_price >= 0.99:
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "wc_current": wc_current,
            "wc_high": wc_high,
            "metar_temp": metar_temp,
            "metar_temp_c": metar_temp_c,
            "diff": round(diff, 2),
            "distance_to_threshold": 0,
            "closing_in": False,
            "in_profit": True,
            "pnl_pct": 0,
            "action": "sell_take_profit",
            "reason": "no_at_99",
        }
        _record_event(position, event, sim)
        logger.info("[%s/%s] NO @ $%.3f >= $0.99, SELL TAKE PROFIT",
                    city_slug, date_str, current_no_price)
        return "sell"

    # 6. Calculate distance to bucket threshold
    bucket_low = position["bucket_low"]
    bucket_high = position["bucket_high"]
    bucket_mid = (bucket_low + bucket_high) / 2
    if bucket_low == -999:
        bucket_mid = bucket_high
    elif bucket_high == 999:
        bucket_mid = bucket_low

    distance_to_threshold = abs(wc_high - bucket_mid)

    # Track METAR trend
    prev_distances = position.get("metar_distances", [])
    prev_distances.append(round(distance_to_threshold, 2))
    if len(prev_distances) > 10:
        prev_distances = prev_distances[-10:]
    position["metar_distances"] = prev_distances

    closing_in = False
    if len(prev_distances) >= METAR_CLOSE_READINGS:
        recent = prev_distances[-METAR_CLOSE_READINGS:]
        closing_in = all(recent[i] > recent[i+1] for i in range(len(recent)-1))

    # Get current P/L
    if current_no_price is not None and current_no_price > 0:
        current_no = current_no_price
    else:
        current_no = position["entry_no_price"]
    pnl_pct = (current_no / position["entry_no_price"] - 1.0) if position["entry_no_price"] > 0 else 0
    in_profit = pnl_pct >= 0

    # ── IMMEDIATE SELL TRIGGERS (bypass proximity gate + trend requirement) ──

    # 7. DAILY HIGH CONFLICT: weather.com daily high now covers the bucket
    #    For a NO position, if the forecast high >= bucket threshold, we're in trouble
    if wc_high is not None:
        # For exact buckets (27°C): daily high >= bucket_low means it could hit
        # For edge buckets ("35°C or higher"): daily high >= 35 means it could hit
        # For edge buckets ("25°C or below"): daily high <= 25 means it could hit
        daily_high_conflict = False
        if bucket_high == 999:
            # "X or higher" bucket — conflict if daily high >= X
            daily_high_conflict = wc_high >= bucket_low
        elif bucket_low == -999:
            # "X or below" bucket — conflict if daily high <= X
            daily_high_conflict = wc_high <= bucket_high
        else:
            # Exact bucket — conflict if daily high == bucket (within tolerance)
            daily_high_conflict = abs(wc_high - bucket_mid) <= 2.0

        if daily_high_conflict:
            event = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "wc_current": wc_current,
                "wc_high": wc_high,
                "metar_temp": metar_temp,
                "metar_temp_c": metar_temp_c,
                "diff": round(diff, 2),
                "distance_to_threshold": round(distance_to_threshold, 2),
                "closing_in": False,
                "running_cool": False,
                "in_profit": in_profit,
                "pnl_pct": round(pnl_pct * 100, 1),
                "action": "sell_forecast_conflict",
                "reason": f"daily_high={wc_high} covers bucket",
            }
            _record_event(position, event, sim)
            logger.info("[%s/%s] DAILY HIGH CONFLICT: wc_high=%.1f covers bucket %.0f-%.0f, SELL",
                        city_slug, date_str, wc_high, bucket_low, bucket_high)
            return "sell"

    # 8. METAR ALREADY AT BUCKET: actual temp hitting the bucket territory
    #    This fires regardless of forecast — reality is already there
    metar_at_bucket = False
    if bucket_high == 999:
        # "X or higher" bucket — METAR at or above X = already hitting
        metar_at_bucket = metar_temp >= bucket_low
    elif bucket_low == -999:
        # "X or below" bucket — METAR at or below X = already hitting
        metar_at_bucket = metar_temp <= bucket_high
    else:
        # Exact bucket — METAR at or above bucket_low = entering danger zone
        metar_at_bucket = metar_temp >= bucket_low

    if metar_at_bucket:
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "wc_current": wc_current,
            "wc_high": wc_high,
            "metar_temp": metar_temp,
            "metar_temp_c": metar_temp_c,
            "diff": round(diff, 2),
            "distance_to_threshold": round(distance_to_threshold, 2),
            "closing_in": closing_in,
            "running_cool": metar_temp < wc_current,
            "in_profit": in_profit,
            "pnl_pct": round(pnl_pct * 100, 1),
            "action": "sell_critical",
            "reason": f"metar={metar_temp} at/above bucket_low={bucket_low}",
        }
        _record_event(position, event, sim)
        logger.info("[%s/%s] METAR AT BUCKET: %.1f >= bucket_low %.0f, SELL",
                    city_slug, date_str, metar_temp, bucket_low)
        return "sell"

    # 9. PROXIMITY GATE: only continue trend analysis when forecast is near the bucket
    if distance_to_threshold > PROXIMITY_THRESHOLD:
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "wc_current": wc_current,
            "wc_high": wc_high,
            "metar_temp": metar_temp,
            "metar_temp_c": metar_temp_c,
            "diff": round(diff, 2),
            "distance_to_threshold": round(distance_to_threshold, 2),
            "closing_in": False,
            "running_cool": False,
            "in_profit": in_profit,
            "pnl_pct": round(pnl_pct * 100, 1),
            "action": "hold",
            "reason": "too_far_from_bucket",
        }
        _record_event(position, event, sim)
        logger.info("[%s/%s] Monitor: wc_high %.1f is %.1f from bucket (too far), holding",
                    city_slug, date_str, wc_high, distance_to_threshold)
        return "hold"

    # 10. How much cooler/warmer than hourly forecast?
    running_cool = metar_temp < wc_current

    # Log monitoring event (with all data)
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "wc_current": wc_current,
        "wc_high": wc_high,
        "metar_temp": metar_temp,
        "metar_temp_c": metar_temp_c,
        "diff": round(diff, 2),
        "distance_to_threshold": round(distance_to_threshold, 2),
        "closing_in": closing_in,
        "running_cool": running_cool,
        "in_profit": in_profit,
        "pnl_pct": round(pnl_pct * 100, 1),
        "action": None,
    }

    # 11. Case analysis (trend-based exits)
    if not closing_in:
        event["action"] = "hold"
        _record_event(position, event, sim)
        logger.info("[%s/%s] Case A: dist=%.1f not shrinking, HOLD (METAR=%.1f, wc_high=%s)",
                    city_slug, date_str, distance_to_threshold, metar_temp,
                    f"{wc_high:.1f}" if wc_high else "N/A")
        return "hold"

    if not in_profit:
        if distance_to_threshold >= DISTANCE_MIN:
            event["action"] = "tighten"
            _record_event(position, event, sim)
            logger.info("[%s/%s] Case C: closing in + loss + dist=%.1f, TIGHTEN",
                        city_slug, date_str, distance_to_threshold)
            return "tighten"

        if pnl_pct <= STOP_LOSS_PCT:
            event["action"] = "sell_stop_loss"
            _record_event(position, event, sim)
            logger.info("[%s/%s] Case D: stop hit (%.1f%%), SELL STOP LOSS",
                        city_slug, date_str, pnl_pct * 100)
            return "sell"

        event["action"] = "wait_for_stop"
        _record_event(position, event, sim)
        logger.info("[%s/%s] Case D: closing in + loss + close, WAIT (pnl=%.1f%%)",
                    city_slug, date_str, pnl_pct * 100)
        return "wait_for_stop"

    # Winning + closing in
    if distance_to_threshold <= 1.0:
        event["action"] = "sell_take_profit"
        _record_event(position, event, sim)
        logger.info("[%s/%s] Case B2: closing in + dist=%.1f <= 1.0, SELL TAKE PROFIT (NO=$%.3f)",
                    city_slug, date_str, distance_to_threshold, current_no)
        return "sell"

    event["action"] = "hold"
    _record_event(position, event, sim)
    logger.info("[%s/%s] Monitor: closing in + dist=%.1f > 1.0, HOLD (NO=$%.3f)",
                city_slug, date_str, distance_to_threshold, current_no)
    return "hold"
