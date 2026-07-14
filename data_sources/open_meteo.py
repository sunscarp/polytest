"""
Open-Meteo API client — free, no auth required.
Provides secondary forecast cross-check for entry decisions.

Uses a local cache to avoid hitting rate limits on repeated scans.
"""

import time
import logging
from datetime import datetime, timezone
from typing import Optional

import requests

from config import REQUEST_TIMEOUT, MAX_RETRIES, RETRY_DELAY

logger = logging.getLogger(__name__)

BASE_URL = "https://api.open-meteo.com/v1/forecast"

# ── Cache (avoids re-fetching same forecast every scan cycle) ─────────────
_CACHE: dict[str, tuple[float, float]] = {}  # key -> (value, expires_at)
_CACHE_TTL = 600  # 10 minutes — forecast data doesn't change that fast

# ── Throttle (spread requests across time to avoid 429s) ─────────────────
_last_request_ts: float = 0.0
_MIN_GAP = 2.5  # seconds between consecutive Open-Meteo requests
_cooldown_until: float = 0.0  # global cooldown after 429


def _cache_key(lat: float, lon: float, target_date: str, unit: str,
               model: str) -> str:
    return f"{lat:.2f},{lon:.2f},{target_date},{unit},{model}"


def _cache_get(key: str) -> Optional[float]:
    entry = _CACHE.get(key)
    if entry and entry[1] > time.time():
        return entry[0]
    _CACHE.pop(key, None)
    return None


def _cache_set(key: str, value: float):
    _CACHE[key] = (value, time.time() + _CACHE_TTL)


def _throttle():
    """Enforce minimum gap between consecutive requests."""
    global _last_request_ts
    now = time.time()
    # Respect global cooldown after 429
    if _cooldown_until > now:
        time.sleep(_cooldown_until - now)
    wait = _MIN_GAP - (time.time() - _last_request_ts)
    if wait > 0:
        time.sleep(wait)
    _last_request_ts = time.time()


def get_daily_high(lat: float, lon: float, target_date: str,
                   unit: str = "celsius", timezone: str = "UTC",
                   model: str = "ecmwf_ifs025") -> Optional[float]:
    """
    Get Open-Meteo's forecast daily high for a specific date.
    Cached for 10 minutes to avoid rate limits.
    """
    # Check cache first
    key = _cache_key(lat, lon, target_date, unit, model)
    cached = _cache_get(key)
    if cached is not None:
        logger.info("Open-Meteo cache hit for %s: %.1f", target_date, cached)
        return cached

    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max",
        "temperature_unit": unit,
        "forecast_days": 7,
        "timezone": timezone,
        "models": model,
        "bias_correction": "true",
    }

    for attempt in range(1, MAX_RETRIES + 1):
        _throttle()
        try:
            resp = requests.get(BASE_URL, params=params, timeout=REQUEST_TIMEOUT)

            # Handle rate limiting with longer backoff
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 30))
                # Set global cooldown so other callers also wait
                global _cooldown_until
                _cooldown_until = time.time() + retry_after + 2
                if attempt < MAX_RETRIES:
                    logger.warning("Open-Meteo 429 rate limited, waiting %ds "
                                   "(attempt %d/%d)", retry_after, attempt, MAX_RETRIES)
                    time.sleep(retry_after)
                    continue
                else:
                    logger.error("Open-Meteo 429 rate limited, all retries exhausted")
                    return None

            resp.raise_for_status()
            data = resp.json()

            if data.get("error"):
                logger.error("Open-Meteo error: %s", data.get("reason", "unknown"))
                return None

            times = data.get("daily", {}).get("time", [])
            temps = data.get("daily", {}).get("temperature_2m_max", [])

            for i, t in enumerate(times):
                if t == target_date and i < len(temps) and temps[i] is not None:
                    temp = temps[i]
                    logger.info("Open-Meteo daily high for %s: %.1f\u00b0%s",
                                target_date, temp,
                                "F" if unit == "fahrenheit" else "C")
                    _cache_set(key, temp)
                    return temp

            logger.warning("Open-Meteo: no data for %s", target_date)
            return None

        except requests.RequestException as e:
            logger.error("Open-Meteo request error (attempt %d/%d): %s",
                         attempt, MAX_RETRIES, e)
            if attempt < MAX_RETRIES:
                backoff = RETRY_DELAY * attempt * 2  # longer backoff on errors
                time.sleep(backoff)

    logger.error("Open-Meteo: all retries exhausted for (%.2f, %.2f)", lat, lon)
    return None


def get_forecast_direction(lat: float, lon: float, target_date: str,
                           threshold: float, unit: str = "celsius",
                           timezone: str = "UTC") -> Optional[dict]:
    """
    Cross-check: does Open-Meteo agree that the threshold is unlikely to hit?

    Returns:
        {"high": float, "above_threshold": bool, "distance": float} or None
    """
    high = get_daily_high(lat, lon, target_date, unit, timezone)
    if high is None:
        return None

    return {
        "high": high,
        "above_threshold": high >= threshold,
        "distance": abs(high - threshold),
    }
