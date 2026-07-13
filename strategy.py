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
    PROXIMITY_THRESHOLD, METAR_CLOSE_READINGS,
)
from data_sources import weather_com, open_meteo, aviation_weather, polymarket

logger = logging.getLogger(__name__)


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

    # Calculate bet size: linear interpolation $1-$3 based on 2-4°C distance
    d = best_signal["distance"]
    bet_size = 1.0 + (d - DISTANCE_MIN) / (DISTANCE_MAX - DISTANCE_MIN) * (3.0 - 1.0)
    bet_size = max(1.0, min(3.0, bet_size))
    best_signal["bet_size"] = round(bet_size, 2)

    logger.info("ENTRY SIGNAL %s/%s: high=%.1f threshold=%.1f dist=%.1f bet=$%.2f NO@$%.3f",
                city_slug, date_str, wc_high, best_signal["threshold"],
                d, bet_size, best_signal["no_price"])

    return best_signal


# ── Monitoring State Machine ──────────────────────────────────────────────

def monitor_position(city_slug: str, station: dict, date_str: str,
                     position: dict, sim,
                     current_no_price: Optional[float] = None) -> Optional[str]:
    """
    Monitor an open position and decide action.

    Strategy: Compare METAR current temp to weather.com hourly forecast.
    If METAR is running consistently BELOW hourly forecast, the day is cooler
    than expected and the actual high may be lower — closer to the bucket.
    Only trigger exit actions when METAR is within PROXIMITY_THRESHOLD of
    the bucket (not just naturally below the daily high at night/morning).

    Args:
        current_no_price: live NO price from Polymarket (for P/L calc).

    Returns:
        Action string: "hold", "sell", "tighten", or "wait_for_stop"
    """
    unit = station["unit"]
    lat, lon = station["lat"], station["lon"]
    icao = station["icao"]

    temp_unit_wc = "e" if unit == "F" else "m"

    # 1. Get weather.com current-hour forecast
    wc_current = weather_com.get_current_hour_temp(lat, lon, temp_unit_wc)
    if wc_current is None:
        logger.warning("[%s/%s] Monitoring: weather.com unavailable, holding", city_slug, date_str)
        return "hold"

    # 2. Get METAR current observation
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

    # 3. Compute diff between sources
    diff = abs(wc_current - metar_temp)

    # 4. Noise check
    if diff < NOISE_THRESHOLD:
        logger.info("[%s/%s] Monitor: diff=%.1f %s (noise), holding",
                    city_slug, date_str, diff, unit)
        return "hold"

    # 5. Calculate distance to bucket threshold
    bucket_mid = (position["bucket_low"] + position["bucket_high"]) / 2
    if position["bucket_low"] == -999:
        bucket_mid = position["bucket_high"]
    elif position["bucket_high"] == 999:
        bucket_mid = position["bucket_low"]

    distance_to_threshold = abs(metar_temp - bucket_mid)

    # 6. PROXIMITY GATE: only trigger exits when actually near the bucket
    # If METAR is far from the bucket, don't act regardless of anything else
    if distance_to_threshold > PROXIMITY_THRESHOLD:
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "wc_current": wc_current,
            "metar_temp": metar_temp,
            "metar_temp_c": metar_temp_c,
            "diff": round(diff, 2),
            "distance_to_threshold": round(distance_to_threshold, 2),
            "closing_in": False,
            "in_profit": False,
            "pnl_pct": 0,
            "action": "hold",
            "reason": "too_far_from_bucket",
        }
        sim.add_monitoring_event(city_slug, date_str, event)
        logger.info("[%s/%s] Monitor: METAR %.1f is %.1f from bucket (too far), holding",
                    city_slug, date_str, metar_temp, distance_to_threshold)
        return "hold"

    # 7. Track METAR trend: is temp moving TOWARD the bucket over time?
    last_metar = position.get("last_metar_temp")
    prev_distances = position.get("metar_distances", [])
    prev_distances.append(round(distance_to_threshold, 2))
    if len(prev_distances) > 10:
        prev_distances = prev_distances[-10:]
    position["metar_distances"] = prev_distances

    # "closing in" = last N readings show distance decreasing
    closing_in = False
    if len(prev_distances) >= METAR_CLOSE_READINGS:
        recent = prev_distances[-METAR_CLOSE_READINGS:]
        closing_in = all(recent[i] > recent[i+1] for i in range(len(recent)-1))

    # 8. How much cooler/warmer than hourly forecast?
    running_cool = metar_temp < wc_current  # actual below hourly forecast

    # Get current P/L
    if current_no_price is not None and current_no_price > 0:
        current_no = current_no_price
    else:
        current_no = position["entry_no_price"]
    pnl_pct = (current_no / position["entry_no_price"] - 1.0) if position["entry_no_price"] > 0 else 0
    in_profit = pnl_pct >= 0

    # Log monitoring event
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "wc_current": wc_current,
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

    # 9. Case analysis
    if not closing_in:
        # Case A: temp NOT trending toward threshold → HOLD
        event["action"] = "hold"
        sim.add_monitoring_event(city_slug, date_str, event)
        logger.info("[%s/%s] Case A: dist=%.1f not shrinking, HOLD (METAR=%.1f)",
                    city_slug, date_str, distance_to_threshold, metar_temp)
        return "hold"

    # Temp IS trending toward threshold and we're near it
    if current_no >= 0.99:
        # Case B: NO at $0.99+ — lock in profit, only 1 cent left to gain
        event["action"] = "sell_take_profit"
        sim.add_monitoring_event(city_slug, date_str, event)
        logger.info("[%s/%s] Case B: NO @ $%.3f >= $0.99, SELL TAKE PROFIT",
                    city_slug, date_str, current_no)
        return "sell"

    if not in_profit:
        if distance_to_threshold >= DISTANCE_MIN:
            # Case C: closing in + loss + still some distance → MONITOR CLOSELY
            event["action"] = "tighten"
            sim.add_monitoring_event(city_slug, date_str, event)
            logger.info("[%s/%s] Case C: closing in + loss + dist=%.1f, TIGHTEN",
                        city_slug, date_str, distance_to_threshold)
            return "tighten"

        # Case D: closing in + loss + very close to bucket → stop loss
        if pnl_pct <= STOP_LOSS_PCT:
            event["action"] = "sell_stop_loss"
            sim.add_monitoring_event(city_slug, date_str, event)
            logger.info("[%s/%s] Case D: stop hit (%.1f%%), SELL STOP LOSS",
                        city_slug, date_str, pnl_pct * 100)
            return "sell"

        event["action"] = "wait_for_stop"
        sim.add_monitoring_event(city_slug, date_str, event)
        logger.info("[%s/%s] Case D: closing in + loss + close, WAIT (pnl=%.1f%%)",
                    city_slug, date_str, pnl_pct * 100)
        return "wait_for_stop"

    # Closing in but NO not at $0.99 yet, and not in loss
    event["action"] = "hold"
    sim.add_monitoring_event(city_slug, date_str, event)
    logger.info("[%s/%s] Monitor: closing in but NO @ $%.3f (need >= $0.99), HOLD",
                city_slug, date_str, current_no)
    return "hold"
