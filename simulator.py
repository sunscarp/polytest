"""
Virtual bankroll and position tracking for the NO trading simulator.
Shared $10 pool across all open positions.
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import SHARED_BANKROLL, LOGS_DIR

logger = logging.getLogger(__name__)


class Simulator:
    """Manages virtual bankroll, open positions, and P/L tracking."""

    def __init__(self, bankroll: float = SHARED_BANKROLL):
        self.starting_bankroll = bankroll
        self.balance = bankroll
        self.open_positions: dict[str, dict] = {}  # keyed by "{city}_{date}"
        self.closed_positions: list[dict] = []
        self._load_state()

    # ── State persistence ─────────────────────────────────────────────

    def _state_path(self) -> Path:
        return LOGS_DIR / "simulator_state.json"

    def _load_state(self):
        path = self._state_path()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.balance = data.get("balance", self.starting_bankroll)
                self.starting_bankroll = data.get("starting_bankroll", self.starting_bankroll)
                self.open_positions = data.get("open_positions", {})
                self.closed_positions = data.get("closed_positions", [])
                logger.info("Loaded state: balance=$%.2f, %d open, %d closed",
                            self.balance, len(self.open_positions), len(self.closed_positions))
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning("Could not load state, starting fresh: %s", e)

    def save_state(self):
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "starting_bankroll": self.starting_bankroll,
            "balance": self.balance,
            "open_positions": self.open_positions,
            "closed_positions": self.closed_positions,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        self._state_path().write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # ── Position lifecycle ────────────────────────────────────────────

    def open_position(self, city_slug: str, date_str: str, bet_size: float,
                      entry_no_price: float, market_id: str, question: str,
                      bucket_range: tuple[float, float],
                      weather_com_high: float, open_meteo_high: Optional[float],
                      distance: float) -> Optional[dict]:
        """
        Open a new NO position. Deducts bet from shared bankroll.

        Returns:
            Position dict if successful, None if insufficient funds or duplicate.
        """
        key = f"{city_slug}_{date_str}"

        if key in self.open_positions:
            logger.warning("Already have open position for %s", key)
            return None

        if bet_size > self.balance:
            logger.warning("Insufficient balance: $%.2f < $%.2f bet", self.balance, bet_size)
            return None

        # Deduct bet from bankroll
        self.balance -= bet_size
        self.balance = round(self.balance, 2)

        position = {
            "city_slug": city_slug,
            "date": date_str,
            "market_id": market_id,
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
            "monitoring_events": [],
        }

        self.open_positions[key] = position
        self.save_state()

        logger.info("OPENED %s: $%.2f NO @ $%.3f | high=%.1f threshold_mid=%.1f dist=%.1f",
                     key, bet_size, entry_no_price,
                     weather_com_high, (bucket_range[0] + bucket_range[1]) / 2, distance)

        # Log to market-specific file
        self._log_market_event(key, "entry", position)
        return position

    def close_position(self, city_slug: str, date_str: str, exit_reason: str,
                       current_no_price: Optional[float] = None) -> Optional[dict]:
        """
        Close an open position. Calculates P/L and adds proceeds back to bankroll.

        Args:
            exit_reason: "take_profit", "stop_loss", "resolution_win", "resolution_loss",
                         "monitor_sell", etc.
            current_no_price: price at exit (for early exits)

        Returns:
            Closed position dict with P/L, or None if no open position.
        """
        key = f"{city_slug}_{date_str}"
        pos = self.open_positions.pop(key, None)
        if not pos:
            return None

        bet = pos["bet_size"]
        entry_no = pos["entry_no_price"]

        if exit_reason == "resolution_win":
            # Market resolved NO — we win
            pnl = round(bet * (1.0 / entry_no - 1.0), 2)
            proceeds = round(bet + pnl, 2)
        elif exit_reason == "resolution_loss":
            # Market resolved YES — we lose the bet
            pnl = -bet
            proceeds = 0.0
        elif current_no_price is not None and current_no_price > 0:
            # Early exit — sell at current NO price
            pnl = round(bet * (current_no_price / entry_no - 1.0), 2)
            proceeds = round(bet + pnl, 2)
            proceeds = max(proceeds, 0.0)  # can't go below 0
        else:
            pnl = 0.0
            proceeds = bet

        # Add proceeds back to bankroll
        self.balance += proceeds
        self.balance = round(self.balance, 2)

        pos["exit_reason"] = exit_reason
        pos["exit_no_price"] = current_no_price
        pos["pnl"] = pnl
        pos["proceeds"] = proceeds
        pos["closed_at"] = datetime.now(timezone.utc).isoformat()
        pos["hold_time_hours"] = round(
            (datetime.fromisoformat(pos["closed_at"]) -
             datetime.fromisoformat(pos["opened_at"])).total_seconds() / 3600, 1
        )

        self.closed_positions.append(pos)
        self.save_state()

        logger.info("CLOSED %s: %s | P/L: $%.2f | balance: $%.2f",
                     key, exit_reason, pnl, self.balance)

        self._log_market_event(key, "exit", pos)
        return pos

    def add_monitoring_event(self, city_slug: str, date_str: str, event: dict):
        """Append a monitoring event to the open position's log."""
        key = f"{city_slug}_{date_str}"
        pos = self.open_positions.get(key)
        if pos:
            pos["monitoring_events"].append(event)
            self.save_state()

    # ── Queries ───────────────────────────────────────────────────────

    def total_unrealized_pnl(self, current_prices: dict[str, float]) -> float:
        """Calculate total unrealized P/L across all open positions."""
        total = 0.0
        for key, pos in self.open_positions.items():
            current_no = current_prices.get(key)
            if current_no is not None and current_no > 0:
                unrealized = pos["bet_size"] * (current_no / pos["entry_no_price"] - 1.0)
                total += unrealized
        return round(total, 2)

    def position_pnl(self, city_slug: str, date_str: str,
                     current_no_price: float) -> float:
        """Calculate unrealized P/L for a single position."""
        key = f"{city_slug}_{date_str}"
        pos = self.open_positions.get(key)
        if not pos or pos["entry_no_price"] <= 0:
            return 0.0
        return round(pos["bet_size"] * (current_no_price / pos["entry_no_price"] - 1.0), 2)

    def has_position(self, city_slug: str, date_str: str) -> bool:
        return f"{city_slug}_{date_str}" in self.open_positions

    def open_count(self) -> int:
        return len(self.open_positions)

    # ── Summary report ────────────────────────────────────────────────

    def summary(self) -> dict:
        """Return summary statistics."""
        total = len(self.closed_positions)
        wins = sum(1 for p in self.closed_positions if (p.get("pnl", 0) or 0) >= 0)
        losses = total - wins
        total_pnl = sum(p.get("pnl", 0) or 0 for p in self.closed_positions)
        avg_hold = (sum(p.get("hold_time_hours", 0) or 0 for p in self.closed_positions)
                    / total if total else 0)

        return {
            "starting_bankroll": self.starting_bankroll,
            "current_balance": self.balance,
            "total_return": round(self.balance - self.starting_bankroll, 2),
            "total_return_pct": round(
                (self.balance - self.starting_bankroll) / self.starting_bankroll * 100, 1
            ),
            "positions_opened": total + len(self.open_positions),
            "positions_closed": total,
            "positions_still_open": len(self.open_positions),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / total * 100, 1) if total else 0,
            "total_pnl": round(total_pnl, 2),
            "avg_hold_hours": round(avg_hold, 1),
        }

    def print_summary(self):
        """Print a formatted summary."""
        s = self.summary()
        print(f"\n{'='*55}")
        print(f"  NO TRADING SIMULATOR — SUMMARY")
        print(f"{'='*55}")
        print(f"  Balance:      ${s['current_balance']:.2f}  "
              f"(start ${s['starting_bankroll']:.2f}, "
              f"{'+'if s['total_return']>=0 else ''}{s['total_return_pct']}%)")
        print(f"  Positions:    {s['positions_opened']} total | "
              f"{s['positions_closed']} closed | {s['positions_still_open']} open")
        if s['positions_closed']:
            print(f"  W/L:          {s['wins']}W / {s['losses']}L  "
                  f"({s['win_rate']}% win rate)")
            print(f"  Total P/L:    {'+'if s['total_pnl']>=0 else ''}${s['total_pnl']:.2f}")
            print(f"  Avg hold:     {s['avg_hold_hours']:.1f}h")
        print(f"{'='*55}\n")

    # ── Logging ───────────────────────────────────────────────────────

    def _log_market_event(self, key: str, event_type: str, data: dict):
        """Append an event to the market-specific log file."""
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOGS_DIR / f"{key}.json"

        events = []
        if log_path.exists():
            try:
                events = json.loads(log_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                events = []

        events.append({
            "type": event_type,
            "ts": datetime.now(timezone.utc).isoformat(),
            **data,
        })

        log_path.write_text(
            json.dumps(events, indent=2, ensure_ascii=False), encoding="utf-8"
        )
