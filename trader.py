"""
Trading layer for Polymarket — Recommendation Mode.

Fetches real on-chain positions, recommends BUYs (no email),
and recommends SELLS for positions you actually own (email sent).
"""

import json
import logging
import smtplib
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import requests

from config import (
    POLY_FUNDER_ADDRESS, LOGS_DIR,
    SMTP_EMAIL, SMTP_PASSWORD, SMTP_RECIPIENT,
    SMTP_SERVER, SMTP_PORT, REQUEST_TIMEOUT, MAX_RETRIES, RETRY_DELAY,
    PAPER_TRADING,
)

logger = logging.getLogger(__name__)


# ── Real Position Fetching ───────────────────────────────────────────────

def get_positions() -> list[dict]:
    """Fetch current positions from the Polymarket Data API."""
    if not POLY_FUNDER_ADDRESS:
        return []
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                "https://data-api.polymarket.com/positions",
                params={"user": POLY_FUNDER_ADDRESS},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.error("Failed to get positions (attempt %d/%d): %s", attempt, MAX_RETRIES, e)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    return []


def match_positions_to_markets(positions: list[dict], weather_markets: list[dict]) -> list[dict]:
    """
    Match real on-chain positions to discovered weather markets.

    Returns list of enriched position dicts ready for monitoring.
    Each contains the real position data + weather market metadata.
    """
    # Build lookup: token_id -> market metadata
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
                    "question": bucket["question"],
                    "bucket_low": bucket["range"][0],
                    "bucket_high": bucket["range"][1],
                    "neg_risk": bucket.get("neg_risk", False),
                }

    matched = []
    for pos in positions:
        token_id = pos.get("asset", pos.get("tokenId", ""))
        outcome = pos.get("outcome", "")
        size = float(pos.get("size", 0))

        if not token_id or size <= 0:
            continue

        # We only care about NO positions
        if outcome.lower() != "no":
            continue

        meta = token_lookup.get(token_id)
        if not meta:
            continue

        avg_price = float(pos.get("avgPrice", pos.get("averagePrice", 0)) or 0)
        current_val = float(pos.get("currentValue", 0) or 0)
        last_price = float(pos.get("lastTradedPrice", 0) or 0)

        # Compute entry NO price from average price if available
        entry_no = avg_price if avg_price > 0 else (1.0 - last_price if last_price > 0 else 0.5)

        matched.append({
            **meta,
            "token_id": token_id,
            "size": size,
            "entry_no_price": entry_no,
            "current_value": current_val,
            "last_price": last_price,
            "bet_size": round(size * entry_no, 2),
            "raw": pos,
        })

    return matched


# ── Monitoring State Cache ────────────────────────────────────────────────

def _monitoring_path() -> Path:
    return LOGS_DIR / "monitoring_state.json"


def load_monitoring_state() -> dict:
    path = _monitoring_path()
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError):
            pass
    return {}


def save_monitoring_state(state: dict):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    _monitoring_path().write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ── Email (Sells Only) ──────────────────────────────────────────────────

def send_email(subject: str, html_body: str) -> bool:
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = SMTP_EMAIL
        msg["To"] = SMTP_RECIPIENT
        msg.attach(MIMEText(f"Recommendation: {subject}", "plain"))
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            server.sendmail(SMTP_EMAIL, SMTP_RECIPIENT, msg.as_string())

        logger.info("Email sent: %s", subject)
        return True
    except Exception as e:
        logger.error("Failed to send email: %s", e)
        return False


def notify_sell(position: dict, reason: str, current_no_price: Optional[float] = None) -> bool:
    """Send a SELL recommendation email for a real position."""
    city = position.get("city_slug", "?").upper()
    date = position.get("date", "?")
    bucket_low = position.get("bucket_low", 0)
    bucket_high = position.get("bucket_high", 0)
    entry_no = position.get("entry_no_price", 0)
    bet_size = position.get("bet_size", 0)
    question = position.get("question", "")
    size = position.get("size", 0)

    exit_price = current_no_price if current_no_price else entry_no
    if entry_no > 0 and exit_price > 0:
        pnl_pct = (exit_price / entry_no - 1.0) * 100
        pnl_dollar = bet_size * (exit_price / entry_no - 1.0)
    else:
        pnl_pct = 0
        pnl_dollar = 0

    pnl_color = "#22c55e" if pnl_dollar >= 0 else "#ef4444"
    result = "WIN" if pnl_dollar >= 0 else "LOSS"

    reason_labels = {
        "monitor_sell": "Monitor Signal",
        "resolution_win": "Resolved YES (Win)",
        "resolution_loss": "Resolved NO (Loss)",
        "manual_sell": "Manual Recommendation",
        "forecast_conflict": "Forecast High Covers Bucket",
        "critical_close": "METAR Critically Close to Bucket",
    }
    reason_label = reason_labels.get(reason, reason)

    subject = f"SELL: {city} {bucket_low}-{bucket_high} ({result} {pnl_dollar:+.2f})"

    html = f"""
    <html>
    <head>
    <style>
        body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #0a0e17; color: #e2e8f0; padding: 20px; }}
        .card {{ background: #111827; border: 1px solid #1e2d45; border-radius: 12px; padding: 24px; max-width: 600px; }}
        .header {{ border-bottom: 2px solid {"#22c55e" if pnl_dollar >= 0 else "#ef4444"}; padding-bottom: 12px; margin-bottom: 16px; }}
        h1 {{ color: {"#22c55e" if pnl_dollar >= 0 else "#ef4444"}; font-size: 18px; margin: 0; }}
        .tag {{ display: inline-block; background: rgba({"34,197,94" if pnl_dollar >= 0 else "239,68,68"},0.15); color: {"#22c55e" if pnl_dollar >= 0 else "#ef4444"}; padding: 3px 10px; border-radius: 4px; font-size: 12px; font-weight: 700; }}
        .row {{ display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #1e2d45; font-size: 14px; }}
        .row:last-child {{ border-bottom: none; }}
        .label {{ color: #64748b; }}
        .value {{ font-weight: 600; font-family: 'Consolas', monospace; }}
        .footer {{ margin-top: 16px; padding-top: 12px; border-top: 1px solid #1e2d45; color: #64748b; font-size: 12px; }}
    </style>
    </head>
    <body>
    <div class="card">
        <div class="header">
            <h1>SELL Recommendation <span class="tag">{result}</span></h1>
        </div>
        <div class="row"><span class="label">City</span><span class="value">{city}</span></div>
        <div class="row"><span class="label">Date</span><span class="value">{date}</span></div>
        <div class="row"><span class="label">Bucket</span><span class="value">{bucket_low} - {bucket_high}</span></div>
        <div class="row"><span class="label">Shares</span><span class="value">{size:.2f}</span></div>
        <div class="row"><span class="label">Entry NO</span><span class="value">${entry_no:.3f}</span></div>
        <div class="row"><span class="label">Current NO</span><span class="value">${exit_price:.3f}</span></div>
        <div class="row"><span class="label">P/L</span><span class="value" style="color:{pnl_color}">{pnl_dollar:+.2f} ({pnl_pct:+.1f}%)</span></div>
        <div class="row"><span class="label">Reason</span><span class="value">{reason_label}</span></div>
        <div class="footer">
            Weather NO Recommender &bull; {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
        </div>
    </div>
    </body>
    </html>
    """

    ok = True
    if PAPER_TRADING:
        logger.info("[PAPER] Sell email suppressed: %s", subject)
    else:
        ok = send_email(subject, html)
    _log_sell_recommendation(position, reason, current_no_price, pnl_dollar, ok)
    return ok


def notify_forecast_drift(position: dict, old_high: float, new_high: float,
                           current_no_price: Optional[float] = None) -> bool:
    """Send a FORECAST DRIFT alert email (informational, not a sell rec)."""
    city = position.get("city_slug", "?").upper()
    date = position.get("date", "?")
    bucket_low = position.get("bucket_low", 0)
    bucket_high = position.get("bucket_high", 0)
    entry_no = position.get("entry_no_price", 0)
    bet_size = position.get("bet_size", 0)

    shift = new_high - old_high

    if current_no_price is not None and current_no_price > 0:
        current_no = current_no_price
    else:
        current_no = entry_no
    if entry_no > 0 and current_no > 0:
        pnl_pct = (current_no / entry_no - 1.0) * 100
        pnl_dollar = bet_size * (current_no / entry_no - 1.0)
    else:
        pnl_pct = 0
        pnl_dollar = 0

    pnl_color = "#22c55e" if pnl_dollar >= 0 else "#ef4444"
    result = "PROFIT" if pnl_dollar >= 0 else "LOSS"

    subject = f"FORECAST SHIFT: {city} {bucket_low}-{bucket_high} ({shift:+.1f}C) [{result} {pnl_dollar:+.2f}]"

    html = f"""
    <html>
    <head>
    <style>
        body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #0a0e17; color: #e2e8f0; padding: 20px; }}
        .card {{ background: #111827; border: 1px solid #1e2d45; border-radius: 12px; padding: 24px; max-width: 600px; }}
        .header {{ border-bottom: 2px solid #f59e0b; padding-bottom: 12px; margin-bottom: 16px; }}
        h1 {{ color: #f59e0b; font-size: 18px; margin: 0; }}
        .tag {{ display: inline-block; background: rgba(245,158,11,0.15); color: #f59e0b; padding: 3px 10px; border-radius: 4px; font-size: 12px; font-weight: 700; }}
        .row {{ display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #1e2d45; font-size: 14px; }}
        .row:last-child {{ border-bottom: none; }}
        .label {{ color: #64748b; }}
        .value {{ font-weight: 600; font-family: 'Consolas', monospace; }}
        .footer {{ margin-top: 16px; padding-top: 12px; border-top: 1px solid #1e2d45; color: #64748b; font-size: 12px; }}
    </style>
    </head>
    <body>
    <div class="card">
        <div class="header">
            <h1>Forecast Drift Alert <span class="tag">SHIFT</span></h1>
        </div>
        <div class="row"><span class="label">City</span><span class="value">{city}</span></div>
        <div class="row"><span class="label">Date</span><span class="value">{date}</span></div>
        <div class="row"><span class="label">Bucket</span><span class="value">{bucket_low} - {bucket_high}</span></div>
        <div class="row"><span class="label">Old Forecast High</span><span class="value">{old_high:.1f}C</span></div>
        <div class="row"><span class="label">New Forecast High</span><span class="value">{new_high:.1f}C</span></div>
        <div class="row"><span class="label">Shift</span><span class="value" style="color:#f59e0b">{shift:+.1f}C</span></div>
        <div class="row"><span class="label">Entry NO</span><span class="value">${entry_no:.3f}</span></div>
        <div class="row"><span class="label">Current NO</span><span class="value">${current_no:.3f}</span></div>
        <div class="row"><span class="label">P/L</span><span class="value" style="color:{pnl_color}">{pnl_dollar:+.2f} ({pnl_pct:+.1f}%)</span></div>
        <div class="footer">
            Weather NO Recommender &bull; {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
        </div>
    </div>
    </body>
    </html>
    """

    ok = True
    if PAPER_TRADING:
        logger.info("[PAPER] Forecast drift email suppressed: %s", subject)
    else:
        ok = send_email(subject, html)
    logger.info("Forecast drift email sent: %s %s (%.1f -> %.1f)", city, date, old_high, new_high)
    _log_sell_recommendation(position, "forecast_drift", current_no_price, pnl_dollar, ok)
    return ok

def _log_sell_recommendation(position: dict, reason: str, current_no_price: Optional[float],
                             pnl: float, email_sent: bool):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / "sell_recommendations.json"

    recs = []
    if log_path.exists():
        try:
            recs = json.loads(log_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError):
            recs = []

    recs.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "email_sent": email_sent,
        "city_slug": position.get("city_slug", ""),
        "date": position.get("date", ""),
        "bucket_low": position.get("bucket_low", 0),
        "bucket_high": position.get("bucket_high", 0),
        "entry_no_price": position.get("entry_no_price", 0),
        "current_no_price": current_no_price,
        "bet_size": position.get("bet_size", 0),
        "size": position.get("size", 0),
        "reason": reason,
        "pnl": pnl,
    })

    if len(recs) > 200:
        recs = recs[-200:]

    log_path.write_text(json.dumps(recs, indent=2, ensure_ascii=False), encoding="utf-8")


def _log_buy_recommendation(signal: dict):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / "buy_recommendations.json"

    recs = []
    if log_path.exists():
        try:
            recs = json.loads(log_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError):
            recs = []

    recs.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "city_slug": signal.get("city_slug", ""),
        "date": signal.get("date", ""),
        "bucket_low": signal.get("bucket_range", (0, 0))[0],
        "bucket_high": signal.get("bucket_range", (0, 0))[1],
        "no_price": signal.get("no_price", 0),
        "yes_price": signal.get("yes_price", 0),
        "bet_size": signal.get("bet_size", 1.0),
        "wc_high": signal.get("wc_high"),
        "om_high": signal.get("om_high"),
        "distance": signal.get("distance", 0),
        "question": signal.get("question", ""),
    })

    if len(recs) > 200:
        recs = recs[-200:]

    log_path.write_text(json.dumps(recs, indent=2, ensure_ascii=False), encoding="utf-8")


def load_buy_recommendations() -> list[dict]:
    log_path = LOGS_DIR / "buy_recommendations.json"
    if log_path.exists():
        try:
            return json.loads(log_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError):
            pass
    return []


def load_sell_recommendations() -> list[dict]:
    log_path = LOGS_DIR / "sell_recommendations.json"
    if log_path.exists():
        try:
            return json.loads(log_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError):
            pass
    return []


# ── Market Cache (token_id -> city/date/bucket mapping) ──────────────────

def _market_cache_path() -> Path:
    return LOGS_DIR / "market_cache.json"


def save_market_cache(weather_markets: list[dict]):
    """Save token_id -> market metadata mapping for dashboard use."""
    cache = load_market_cache()
    for wm in weather_markets:
        for bucket in wm.get("buckets", []):
            tid = bucket.get("token_id", "")
            if tid:
                cache[tid] = {
                    "city_slug": wm["city_slug"],
                    "date_str": wm["date_str"],
                    "market_id": bucket["market_id"],
                    "question": bucket["question"],
                    "bucket_low": bucket["range"][0],
                    "bucket_high": bucket["range"][1],
                }
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    _market_cache_path().write_text(
        json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def load_market_cache() -> dict:
    """Load token_id -> market metadata cache."""
    path = _market_cache_path()
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError):
            pass
    return {}


def get_enriched_positions() -> list[dict]:
    """
    Fetch real positions and enrich with market metadata from cache.
    Returns positions labeled with city/date/bucket.
    """
    positions = get_positions()
    cache = load_market_cache()

    enriched = []
    for pos in positions:
        token_id = pos.get("asset", pos.get("tokenId", ""))
        outcome = pos.get("outcome", "")
        size = float(pos.get("size", 0) or 0)

        if size <= 0:
            continue

        avg_price = float(pos.get("avgPrice", pos.get("averagePrice", 0)) or 0)
        current_val = float(pos.get("currentValue", 0) or 0)
        last_price = float(pos.get("lastTradedPrice", 0) or 0)
        cost_basis = size * avg_price

        meta = cache.get(token_id, {})
        city_slug = meta.get("city_slug", "")
        date_str = meta.get("date_str", "")
        bucket_low = meta.get("bucket_low", 0)
        bucket_high = meta.get("bucket_high", 0)

        if city_slug:
            label = f"{city_slug.upper()} {date_str} ({bucket_low}-{bucket_high})"
        elif outcome.lower() == "no":
            label = f"NO {token_id[:12]}..."
        else:
            label = f"{outcome} {token_id[:12]}..."

        pnl = current_val - cost_basis if cost_basis > 0 else 0
        pnl_pct = (last_price / avg_price - 1.0) * 100 if avg_price > 0 and last_price > 0 else 0

        enriched.append({
            "token_id": token_id,
            "outcome": outcome,
            "label": label,
            "city_slug": city_slug,
            "date_str": date_str,
            "bucket_low": bucket_low,
            "bucket_high": bucket_high,
            "market_id": meta.get("market_id", ""),
            "question": meta.get("question", ""),
            "size": size,
            "avg_price": avg_price,
            "last_price": last_price,
            "current_value": current_val,
            "cost_basis": round(cost_basis, 4),
            "pnl": round(pnl, 4),
            "pnl_pct": round(pnl_pct, 1),
            "condition_id": pos.get("conditionId", pos.get("condition_id", "")),
        })

    # Sort: weather-labeled first, then by P/L
    enriched.sort(key=lambda p: (not bool(p["city_slug"]), -p["pnl"]))
    return enriched
