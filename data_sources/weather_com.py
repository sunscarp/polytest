"""
weather.com data source — uses The Weather Company (TWC) API.

The TWC API provides hourly and daily forecasts for any geocode.
Rate limit: moderate — includes retries and rate limiting.
API key: embedded (public, from weather.com's own frontend).
"""

import time
import logging
from datetime import datetime, timezone
from typing import Optional

import requests

from config import REQUEST_TIMEOUT, MAX_RETRIES, RETRY_DELAY

logger = logging.getLogger(__name__)

# TWC API key (from weather.com's frontend assets)
API_KEY = "6532d6454b8aa370768e63d6ba5a832e"
BASE_URL = "https://api.weather.com/v3/wx"

# Rate limiting
_last_request_time = 0.0
_MIN_DELAY = 0.5


def _rate_limit():
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < _MIN_DELAY:
        time.sleep(_MIN_DELAY - elapsed)
    _last_request_time = time.time()


def _get(endpoint: str, lat: float, lon: float, units: str = "e") -> Optional[dict]:
    """Make a TWC API request."""
    _rate_limit()

    url = f"{BASE_URL}/{endpoint}"
    params = {
        "geocode": f"{lat:.4f},{lon:.4f}",
        "units": units,
        "language": "en-US",
        "format": "json",
        "apiKey": API_KEY,
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            logger.error("TWC API error (attempt %d/%d): %s", attempt, MAX_RETRIES, e)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)

    logger.error("TWC API: all retries exhausted for %s (%.2f, %.2f)", endpoint, lat, lon)
    return None


def get_daily_high(lat: float, lon: float, target_date: str,
                   unit: str = "e") -> Optional[float]:
    """
    Get forecast daily high for a specific date.

    Args:
        lat, lon: coordinates
        target_date: "YYYY-MM-DD"
        unit: "e" (imperial/F) or "m" (metric/C)

    Returns:
        Forecast daily high temperature, or None on failure.
    """
    data = _get("forecast/daily/5day", lat, lon, unit)
    if not data:
        return None

    highs = data.get("temperatureMax", [])
    times = data.get("validTimeLocal", [])

    for i, high in enumerate(highs):
        if i < len(times) and times[i]:
            date_str = times[i][:10]
            if date_str == target_date and high is not None:
                unit_sym = "F" if unit == "e" else "C"
                logger.info("TWC daily high for %s: %.1f°%s", target_date, high, unit_sym)
                return float(high)

    logger.warning("TWC: no daily high found for %s", target_date)
    return None


def get_current_hour_temp(lat: float, lon: float, unit: str = "e") -> Optional[float]:
    """
    Get the current-hour forecast temperature.

    Args:
        lat, lon: coordinates
        unit: "e" (imperial/F) or "m" (metric/C)

    Returns:
        Current-hour forecast temp, or None on failure.
    """
    data = _get("forecast/hourly/2day", lat, lon, unit)
    if not data:
        return None

    temps = data.get("temperature", [])
    times = data.get("validTimeLocal", [])

    if not temps:
        logger.warning("TWC: no hourly temperature data")
        return None

    # Find the hour closest to now
    now = datetime.now(timezone.utc)
    best_idx = 0
    best_diff = float("inf")

    for i, t_str in enumerate(times):
        try:
            t = datetime.fromisoformat(t_str)
            diff = abs((t - now).total_seconds())
            if diff < best_diff:
                best_diff = diff
                best_idx = i
        except (ValueError, TypeError):
            continue

    temp = temps[best_idx]
    unit_sym = "F" if unit == "e" else "C"
    logger.info("TWC current-hour temp: %.1f°%s (index %d)", temp, unit_sym, best_idx)
    return float(temp)


def get_hourly_forecast(lat: float, lon: float, unit: str = "e") -> list[dict]:
    """
    Get full hourly forecast.

    Returns:
        List of {"time": str, "temp": float} dicts.
    """
    data = _get("forecast/hourly/2day", lat, lon, unit)
    if not data:
        return []

    temps = data.get("temperature", [])
    times = data.get("validTimeLocal", [])

    result = []
    for i, temp in enumerate(temps):
        if temp is not None and i < len(times):
            result.append({"time": times[i], "temp": float(temp)})
    return result
