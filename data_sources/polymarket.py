"""
Polymarket Gamma API client — read-only market data for temperature markets.
No auth required. Rate limit: 300 requests/10s for /markets, 500/10s for /events.
"""

import json
import time
import logging
from typing import Optional

import requests

from config import REQUEST_TIMEOUT, MAX_RETRIES, RETRY_DELAY

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"

MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december"
]


def _slug_for_date(city_slug: str, date_str: str) -> str:
    """
    Build the Polymarket event slug for a city/date.

    Args:
        city_slug: e.g. "nyc", "london", "tokyo"
        date_str: "YYYY-MM-DD"

    Returns:
        e.g. "highest-temperature-in-nyc-on-july-13-2026"
    """
    parts = date_str.split("-")
    year = parts[0]
    month = MONTHS[int(parts[1]) - 1]
    day = int(parts[2])
    return f"highest-temperature-in-{city_slug}-on-{month}-{day}-{year}"


def _parse_temp_range(question: str) -> tuple[float, float]:
    """
    Parse temperature range from a Polymarket market question.

    Examples:
        "Will the highest temperature in NYC be between 86-87°F on ..." -> (86, 87)
        "Will the highest temperature in NYC be 27°C on ..." -> (27, 27)
        "Will the highest temperature in NYC be 77°F or below on ..." -> (-999, 77)
        "Will the highest temperature in NYC be 96°F or higher on ..." -> (96, 999)
    """
    import re

    # "between X-Y°F" or "between X-Y°C"
    m = re.search(r'between\s+(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*°', question)
    if m:
        return (float(m.group(1)), float(m.group(2)))

    # "X°F or below" / "X°C or below"
    m = re.search(r'(\d+(?:\.\d+)?)\s*°[FC]\s+or\s+below', question, re.IGNORECASE)
    if m:
        return (-999.0, float(m.group(1)))

    # "X°F or higher" / "X°C or higher"
    m = re.search(r'(\d+(?:\.\d+)?)\s*°[FC]\s+or\s+higher', question, re.IGNORECASE)
    if m:
        return (float(m.group(1)), 999.0)

    # "be X°C on" / "be X°F on" (exact single degree)
    m = re.search(r'be\s+(\d+(?:\.\d+)?)\s*°[FC]\s+on', question, re.IGNORECASE)
    if m:
        val = float(m.group(1))
        return (val, val)

    return (0.0, 0.0)


def get_event(city_slug: str, date_str: str) -> Optional[dict]:
    """
    Fetch a Polymarket temperature event by city and date.

    Returns:
        Event dict with "markets" array, or None if not found.
    """
    slug = _slug_for_date(city_slug, date_str)
    url = f"{GAMMA_BASE}/events"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params={"slug": slug}, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()

            if data and isinstance(data, list) and len(data) > 0:
                event = data[0]
                logger.info("Polymarket event found: %s (%d markets)",
                            slug, len(event.get("markets", [])))
                return event

            logger.info("Polymarket: no event for %s", slug)
            return None

        except requests.RequestException as e:
            logger.error("Polymarket event request error (attempt %d/%d): %s",
                         attempt, MAX_RETRIES, e)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)

    return None


def get_market_price(market_id: str) -> Optional[dict]:
    """
    Fetch current prices for a specific market.

    Returns:
        {"yes_price": float, "no_price": float, "bid": float, "ask": float,
         "volume": float, "closed": bool} or None
    """
    url = f"{GAMMA_BASE}/markets/{market_id}"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            mdata = resp.json()

            prices_raw = mdata.get("outcomePrices", "[0.5,0.5]")
            prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw

            yes_price = float(prices[0]) if len(prices) > 0 else 0.5
            no_price = float(prices[1]) if len(prices) > 1 else 0.5

            return {
                "yes_price": yes_price,
                "no_price": no_price,
                "bid": float(mdata.get("bestBid", 0)),
                "ask": float(mdata.get("bestAsk", 1)),
                "volume": float(mdata.get("volume", 0)),
                "closed": mdata.get("closed", False),
                "active": mdata.get("active", True),
            }

        except requests.RequestException as e:
            logger.error("Polymarket market request error (attempt %d/%d): %s",
                         attempt, MAX_RETRIES, e)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)

    return None


def get_city_buckets(city_slug: str, date_str: str) -> list[dict]:
    """
    Get all temperature buckets for a city/date with current prices.

    Returns:
        List of dicts sorted by temperature range:
        [{"market_id": str, "question": str, "range": (low, high),
          "yes_price": float, "no_price": float, "volume": float}, ...]
    """
    event = get_event(city_slug, date_str)
    if not event:
        return []

    buckets = []
    for market in event.get("markets", []):
        question = market.get("question", "")
        t_low, t_high = _parse_temp_range(question)

        # Parse prices from the event data
        prices_raw = market.get("outcomePrices", "[0.5,0.5]")
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw

        yes_price = float(prices[0]) if len(prices) > 0 else 0.5
        no_price = float(prices[1]) if len(prices) > 1 else 0.5
        volume = float(market.get("volume", 0))

        buckets.append({
            "market_id": market.get("id", ""),
            "question": question,
            "range": (t_low, t_high),
            "yes_price": yes_price,
            "no_price": no_price,
            "volume": volume,
            "active": market.get("active", True),
            "closed": market.get("closed", False),
        })

    # Sort by lower bound of temperature range
    buckets.sort(key=lambda b: b["range"][0])
    return buckets
