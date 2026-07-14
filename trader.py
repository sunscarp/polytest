"""
Live CLOB trading execution layer for Polymarket.

Uses py-clob-client-v2 to place real orders on the CLOB.
All positions are also tracked locally in simulator_state.json
for dashboard display and P/L tracking.
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from py_clob_client_v2 import (
    ClobClient,
    OrderArgs,
    MarketOrderArgs,
    OrderType,
    Side,
    PartialCreateOrderOptions,
    ApiCreds,
)

from config import (
    POLY_PRIVATE_KEY, POLY_FUNDER_ADDRESS, POLY_SIGNATURE_TYPE,
    POLY_CHAIN_ID, POLY_CLOB_HOST, LOGS_DIR, MIN_BET, MAX_BET,
)

logger = logging.getLogger(__name__)

_client: Optional[ClobClient] = None
_api_creds: Optional[ApiCreds] = None


def get_client() -> ClobClient:
    """Get or create the authenticated CLOB client."""
    global _client, _api_creds

    if _client is not None:
        return _client

    if not POLY_PRIVATE_KEY:
        raise ValueError("POLY_PRIVATE_KEY not set in .env")

    logger.info("Initializing Polymarket CLOB client (chain=%d, sig_type=%d)",
                POLY_CHAIN_ID, POLY_SIGNATURE_TYPE)

    # Step 1: L1 auth — derive API credentials from private key
    temp_client = ClobClient(
        host=POLY_CLOB_HOST,
        chain_id=POLY_CHAIN_ID,
        key=POLY_PRIVATE_KEY,
    )

    try:
        creds = temp_client.create_or_derive_api_key()
        logger.info("API credentials derived successfully")
    except Exception as e:
        logger.error("Failed to derive API credentials: %s", e)
        raise

    # Step 2: L2 auth — fully authenticated client
    _client = ClobClient(
        host=POLY_CLOB_HOST,
        chain_id=POLY_CHAIN_ID,
        key=POLY_PRIVATE_KEY,
        creds=creds,
        signature_type=POLY_SIGNATURE_TYPE,
        funder=POLY_FUNDER_ADDRESS if POLY_FUNDER_ADDRESS else None,
    )
    _api_creds = creds

    return _client


def buy_no_tokens(token_id: str, price: float, size: float,
                  neg_risk: bool = False) -> Optional[dict]:
    """
    Place a limit buy order for NO tokens.

    Args:
        token_id: The CLOB token ID for the NO outcome
        price: Limit price per share (what we pay)
        size: Number of shares to buy
        neg_risk: Whether this is a neg-risk market

    Returns:
        Order response dict or None on failure
    """
    client = get_client()

    try:
        tick_size = _get_tick_size(price)

        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            side=Side.BUY,
            size=size,
        )

        options = PartialCreateOrderOptions(
            tick_size=tick_size,
            neg_risk=neg_risk,
        )

        resp = client.create_and_post_order(
            order_args=order_args,
            options=options,
            order_type=OrderType.GTC,
        )

        logger.info("BUY order placed: token=%s price=%.3f size=%.2f | resp=%s",
                     token_id, price, size, resp)
        return resp

    except Exception as e:
        logger.error("Failed to place BUY order: %s", e)
        return None


def buy_no_market(token_id: str, amount_usdc: float,
                  neg_risk: bool = False) -> Optional[dict]:
    """
    Place a market buy order for NO tokens (FOK — fill or kill).

    Args:
        token_id: The CLOB token ID for the NO outcome
        amount_usdc: How much USDC to spend
        neg_risk: Whether this is a neg-risk market

    Returns:
        Order response dict or None on failure
    """
    client = get_client()

    try:
        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=amount_usdc,
            side=Side.BUY,
            order_type=OrderType.FOK,
        )

        options = PartialCreateOrderOptions(
            tick_size="0.01",
            neg_risk=neg_risk,
        )

        resp = client.create_and_post_market_order(
            order_args=order_args,
            options=options,
            order_type=OrderType.FOK,
        )

        logger.info("MARKET BUY: token=%s amount=$%.2f | resp=%s",
                     token_id, amount_usdc, resp)
        return resp

    except Exception as e:
        logger.error("Failed to place MARKET BUY: %s", e)
        return None


def sell_no_tokens(token_id: str, price: float, size: float,
                   neg_risk: bool = False) -> Optional[dict]:
    """
    Place a limit sell order for NO tokens.

    Args:
        token_id: The CLOB token ID for the NO outcome
        price: Limit price per share
        size: Number of shares to sell
        neg_risk: Whether this is a neg-risk market

    Returns:
        Order response dict or None on failure
    """
    client = get_client()

    try:
        tick_size = _get_tick_size(price)

        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            side=Side.SELL,
            size=size,
        )

        options = PartialCreateOrderOptions(
            tick_size=tick_size,
            neg_risk=neg_risk,
        )

        resp = client.create_and_post_order(
            order_args=order_args,
            options=options,
            order_type=OrderType.GTC,
        )

        logger.info("SELL order placed: token=%s price=%.3f size=%.2f | resp=%s",
                     token_id, price, size, resp)
        return resp

    except Exception as e:
        logger.error("Failed to place SELL order: %s", e)
        return None


def sell_no_market(token_id: str, size: float,
                   neg_risk: bool = False) -> Optional[dict]:
    """
    Place a market sell order for NO tokens (FOK — fill or kill).

    Args:
        token_id: The CLOB token ID for the NO outcome
        size: Number of shares to sell
        neg_risk: Whether this is a neg-risk market

    Returns:
        Order response dict or None on failure
    """
    client = get_client()

    try:
        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=size,
            side=Side.SELL,
            order_type=OrderType.FOK,
        )

        options = PartialCreateOrderOptions(
            tick_size="0.01",
            neg_risk=neg_risk,
        )

        resp = client.create_and_post_market_order(
            order_args=order_args,
            options=options,
            order_type=OrderType.FOK,
        )

        logger.info("MARKET SELL: token=%s size=%.2f | resp=%s",
                     token_id, size, resp)
        return resp

    except Exception as e:
        logger.error("Failed to place MARKET SELL: %s", e)
        return None


def cancel_order(order_id: str) -> Optional[dict]:
    """Cancel a specific order by ID."""
    client = get_client()
    try:
        resp = client.cancel(order_id)
        logger.info("Cancelled order %s: %s", order_id, resp)
        return resp
    except Exception as e:
        logger.error("Failed to cancel order %s: %s", order_id, e)
        return None


def cancel_all_orders() -> Optional[dict]:
    """Cancel all open orders."""
    client = get_client()
    try:
        resp = client.cancel_all()
        logger.info("Cancelled all orders: %s", resp)
        return resp
    except Exception as e:
        logger.error("Failed to cancel all orders: %s", e)
        return None


def get_open_orders() -> list[dict]:
    """Fetch all open orders from the CLOB."""
    client = get_client()
    try:
        resp = client.get_open_orders()
        if isinstance(resp, list):
            return resp
        return resp.get("data", []) if isinstance(resp, dict) else []
    except Exception as e:
        logger.error("Failed to get open orders: %s", e)
        return []


def get_order_book(token_id: str) -> Optional[dict]:
    """Fetch the order book for a token."""
    client = get_client()
    try:
        return client.get_order_book(token_id)
    except Exception as e:
        logger.error("Failed to get order book for %s: %s", token_id, e)
        return None


def get_midpoint(token_id: str) -> Optional[float]:
    """Get the midpoint price for a token."""
    client = get_client()
    try:
        resp = client.get_midpoint(token_id)
        return float(resp) if resp else None
    except Exception as e:
        logger.error("Failed to get midpoint for %s: %s", token_id, e)
        return None


def get_price(token_id: str, side: str = "BUY") -> Optional[float]:
    """Get the best price for a token on a given side."""
    client = get_client()
    try:
        resp = client.get_price(token_id, side=side)
        return float(resp) if resp else None
    except Exception as e:
        logger.error("Failed to get price for %s: %s", token_id, e)
        return None


def get_wallet_balance() -> Optional[float]:
    """
    Get the USDC/pUSD balance for the funder wallet.
    Returns balance in USD or None on failure.
    """
    client = get_client()
    try:
        from py_clob_client_v2.clob_types import BalanceAllowanceParams
        params = BalanceAllowanceParams(asset_type="COLLATERAL")
        resp = client.get_balance_allowance(params)
        if isinstance(resp, dict):
            raw_balance = resp.get("balance", "0")
            return float(raw_balance) / 1_000_000
        return None
    except Exception as e:
        logger.error("Failed to get wallet balance: %s", e)
        return None


def get_positions() -> list[dict]:
    """Fetch current positions from the Data API."""
    try:
        import httpx
        resp = httpx.get(
            f"https://data-api.polymarket.com/positions",
            params={"user": POLY_FUNDER_ADDRESS},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error("Failed to get positions: %s", e)
        return []


def _get_tick_size(price: float) -> str:
    """Determine tick size based on price level."""
    if price < 0.10:
        return "0.001"
    elif price < 0.50:
        return "0.01"
    else:
        return "0.01"


# ── Local Position Tracking ──────────────────────────────────────────────

def _state_path() -> Path:
    return LOGS_DIR / "simulator_state.json"


def load_state() -> dict:
    """Load local position tracking state."""
    path = _state_path()
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError):
            pass
    return {
        "starting_bankroll": 3.0,
        "balance": 3.0,
        "open_positions": {},
        "closed_positions": [],
    }


def save_state(state: dict):
    """Save local position tracking state."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    state["saved_at"] = datetime.now(timezone.utc).isoformat()
    _state_path().write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def record_entry(state: dict, city_slug: str, date_str: str,
                 bet_size: float, entry_no_price: float, market_id: str,
                 token_id: str, question: str, bucket_range: tuple,
                 weather_com_high: float, open_meteo_high: Optional[float],
                 distance: float, order_resp: dict) -> dict:
    """Record a new position in local state after a live order is placed."""
    key = f"{city_slug}_{date_str}"

    if key in state["open_positions"]:
        logger.warning("Already tracking position for %s", key)
        return state["open_positions"][key]

    # Deduct from balance
    state["balance"] = round(state["balance"] - bet_size, 2)

    position = {
        "city_slug": city_slug,
        "date": date_str,
        "market_id": market_id,
        "token_id": token_id,
        "question": question,
        "bucket_low": bucket_range[0],
        "bucket_high": bucket_range[1],
        "bet_size": round(bet_size, 2),
        "entry_no_price": entry_no_price,
        "entry_yes_price": round(1 - entry_no_price, 4),
        "shares": round(bet_size / entry_no_price, 2) if entry_no_price > 0 else 0,
        "weather_com_high": weather_com_high,
        "open_meteo_high": open_meteo_high,
        "distance": round(distance, 2),
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "order_id": order_resp.get("orderID", order_resp.get("id", "")),
        "monitoring_events": [],
    }

    state["open_positions"][key] = position
    save_state(state)

    logger.info("RECORDED ENTRY %s: $%.2f NO @ $%.3f | order_id=%s",
                key, bet_size, entry_no_price, position["order_id"])
    return position


def record_exit(state: dict, city_slug: str, date_str: str,
                exit_reason: str, current_no_price: Optional[float] = None,
                order_resp: Optional[dict] = None) -> Optional[dict]:
    """Record a position exit in local state after a live sell order."""
    key = f"{city_slug}_{date_str}"
    pos = state["open_positions"].pop(key, None)
    if not pos:
        return None

    bet = pos["bet_size"]
    entry_no = pos["entry_no_price"]

    if exit_reason == "resolution_win":
        pnl = round(bet * (1.0 / entry_no - 1.0), 2)
        proceeds = round(bet + pnl, 2)
    elif exit_reason == "resolution_loss":
        pnl = -bet
        proceeds = 0.0
    elif current_no_price is not None and current_no_price > 0:
        pnl = round(bet * (current_no_price / entry_no - 1.0), 2)
        proceeds = round(bet + pnl, 2)
        proceeds = max(proceeds, 0.0)
    else:
        pnl = 0.0
        proceeds = bet

    state["balance"] = round(state["balance"] + proceeds, 2)

    pos["exit_reason"] = exit_reason
    pos["exit_no_price"] = current_no_price
    pos["pnl"] = pnl
    pos["proceeds"] = proceeds
    pos["closed_at"] = datetime.now(timezone.utc).isoformat()
    pos["hold_time_hours"] = round(
        (datetime.fromisoformat(pos["closed_at"]) -
         datetime.fromisoformat(pos["opened_at"])).total_seconds() / 3600, 1
    )
    if order_resp:
        pos["exit_order_id"] = order_resp.get("orderID", order_resp.get("id", ""))

    state["closed_positions"].append(pos)
    save_state(state)

    logger.info("RECORDED EXIT %s: %s | P/L: $%.2f | balance: $%.2f",
                key, exit_reason, pnl, state["balance"])
    return pos
