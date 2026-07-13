"""
Open-Meteo API client — free, no auth required.
Provides secondary forecast cross-check for entry decisions.
"""

import time
import logging
from typing import Optional

import requests

from config import REQUEST_TIMEOUT, MAX_RETRIES, RETRY_DELAY

logger = logging.getLogger(__name__)

BASE_URL = "https://api.open-meteo.com/v1/forecast"


def get_daily_high(lat: float, lon: float, target_date: str,
                   unit: str = "celsius", timezone: str = "UTC",
                   model: str = "ecmwf_ifs025") -> Optional[float]:
    """
    Get Open-Meteo's forecast daily high for a specific date.

    Args:
        lat, lon: coordinates
        target_date: "YYYY-MM-DD"
        unit: "celsius" or "fahrenheit"
        timezone: IANA timezone string
        model: forecast model (ecmwf_ifs025, gfs_seamless, etc.)

    Returns:
        Forecast daily high temperature, or None on failure.
    """
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
        try:
            resp = requests.get(BASE_URL, params=params, timeout=REQUEST_TIMEOUT)
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
                    logger.info("Open-Meteo daily high for %s: %.1f°%s",
                                target_date, temp,
                                "F" if unit == "fahrenheit" else "C")
                    return temp

            logger.warning("Open-Meteo: no data for %s", target_date)
            return None

        except requests.RequestException as e:
            logger.error("Open-Meteo request error (attempt %d/%d): %s",
                         attempt, MAX_RETRIES, e)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)

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
