"""
aviationweather.gov METAR client — free, no auth required.
Provides real-time observed temperature at airport stations for live monitoring.
"""

import time
import logging
from typing import Optional

import requests

from config import REQUEST_TIMEOUT, MAX_RETRIES, RETRY_DELAY

logger = logging.getLogger(__name__)

BASE_URL = "https://aviationweather.gov/api/data/metar"


def get_current_temp(icao: str, hours_back: float = 1.5) -> Optional[dict]:
    """
    Get the latest METAR observation for a station.

    Args:
        icao: ICAO station code (e.g. "KLGA", "ZSPD")
        hours_back: how far back to search (default 1.5 hours)

    Returns:
        {
            "temp_c": float,       # current temp in Celsius
            "temp_f": float,       # convenience Fahrenheit conversion
            "report_time": str,    # ISO timestamp
            "raw_ob": str,         # raw METAR text
            "flight_cat": str,     # VFR/MVFR/IFR/LIFR
        } or None on failure
    """
    params = {
        "ids": icao,
        "format": "json",
        "hours": hours_back,
    }

    headers = {
        "User-Agent": "WeatherNoSim/1.0",
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(BASE_URL, params=params, headers=headers,
                                timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()

            if not data or not isinstance(data, list) or len(data) == 0:
                logger.warning("METAR: no data returned for %s", icao)
                return None

            obs = data[0]
            temp_c = obs.get("temp")
            if temp_c is None:
                logger.warning("METAR: no temperature in observation for %s", icao)
                return None

            temp_f = round(temp_c * 9 / 5 + 32, 1)

            result = {
                "temp_c": temp_c,
                "temp_f": temp_f,
                "report_time": obs.get("reportTime", ""),
                "raw_ob": obs.get("rawOb", ""),
                "flight_cat": obs.get("fltCat", ""),
            }

            logger.info("METAR %s: %.1f°C / %.1f°F (reported %s)",
                        icao, temp_c, temp_f, result["report_time"])
            return result

        except requests.RequestException as e:
            logger.error("METAR request error for %s (attempt %d/%d): %s",
                         icao, attempt, MAX_RETRIES, e)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)

    logger.error("METAR: all retries exhausted for %s", icao)
    return None


def get_multiple_stations(icao_list: list[str],
                          hours_back: float = 1.5) -> dict[str, dict]:
    """
    Fetch METAR for multiple stations in one request.

    Returns:
        {icao: {temp_c, temp_f, ...}} dict
    """
    if not icao_list:
        return {}

    params = {
        "ids": ",".join(icao_list),
        "format": "json",
        "hours": hours_back,
    }

    headers = {"User-Agent": "WeatherNoSim/1.0"}

    try:
        resp = requests.get(BASE_URL, params=params, headers=headers,
                            timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        results = {}
        for obs in data:
            icao = obs.get("icaoId", "")
            temp_c = obs.get("temp")
            if temp_c is not None:
                results[icao] = {
                    "temp_c": temp_c,
                    "temp_f": round(temp_c * 9 / 5 + 32, 1),
                    "report_time": obs.get("reportTime", ""),
                    "raw_ob": obs.get("rawOb", ""),
                    "flight_cat": obs.get("fltCat", ""),
                }
        return results

    except requests.RequestException as e:
        logger.error("METAR batch request error: %s", e)
        return {}
