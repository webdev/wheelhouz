"""Alpaca paper trading client — connects PaperTrader to Alpaca API.

Uses Alpaca paper trading API for realistic order simulation.
Base URL: https://paper-api.alpaca.markets
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any


@dataclass
class AlpacaConfig:
    """Alpaca API configuration."""
    api_key: str = ""
    api_secret: str = ""
    base_url: str = "https://paper-api.alpaca.markets"
    options_enabled: bool = True


@dataclass
class AlpacaOrder:
    """An order submitted to Alpaca."""
    order_id: str
    symbol: str
    side: str  # "sell_to_open", "buy_to_close"
    order_type: str  # "limit", "market"
    quantity: int
    limit_price: Decimal | None = None
    status: str = "pending"  # "pending", "filled", "cancelled", "rejected"
    filled_price: Decimal | None = None
    filled_at: datetime | None = None
    time_in_force: str = "day"


@dataclass
class AlpacaPosition:
    """A position from Alpaca account."""
    symbol: str
    quantity: int
    avg_entry_price: Decimal
    current_price: Decimal
    unrealized_pnl: Decimal
    market_value: Decimal


@dataclass
class AlpacaAccount:
    """Alpaca paper trading account state."""
    equity: Decimal = Decimal("0")
    buying_power: Decimal = Decimal("0")
    cash: Decimal = Decimal("0")
    portfolio_value: Decimal = Decimal("0")
    positions: list[AlpacaPosition] = field(default_factory=list)
    open_orders: list[AlpacaOrder] = field(default_factory=list)


class AlpacaPaperClient:
    """Client for Alpaca paper trading API.

    In production: uses alpaca-py SDK.
    For testing: operates on in-memory state.
    """

    def __init__(self, config: AlpacaConfig | None = None) -> None:
        self.config = config or AlpacaConfig()
        self._account = AlpacaAccount(
            equity=Decimal("100000"),
            buying_power=Decimal("100000"),
            cash=Decimal("100000"),
            portfolio_value=Decimal("100000"),
        )
        self._orders: list[AlpacaOrder] = []
        self._positions: dict[str, AlpacaPosition] = {}
        self._next_order_id = 1

    def get_account(self) -> AlpacaAccount:
        """Get current account state."""
        self._account.positions = list(self._positions.values())
        self._account.open_orders = [
            o for o in self._orders if o.status == "pending"
        ]
        return self._account

    def submit_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str = "limit",
        limit_price: Decimal | None = None,
        time_in_force: str = "day",
    ) -> AlpacaOrder:
        """Submit an order to Alpaca paper trading.

        For sell_to_open (short put/call): receives premium.
        For buy_to_close: pays to close position.
        """
        order_id = f"ALP-{self._next_order_id:06d}"
        self._next_order_id += 1

        order = AlpacaOrder(
            order_id=order_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            limit_price=limit_price,
            time_in_force=time_in_force,
        )

        # Simulate immediate fill for paper trading
        if order_type == "market" or limit_price is not None:
            fill_price = limit_price or Decimal("0")
            order.status = "filled"
            order.filled_price = fill_price
            order.filled_at = datetime.utcnow()

            # Update position
            self._update_position_from_fill(symbol, side, quantity, fill_price)

        self._orders.append(order)
        return order

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order."""
        for order in self._orders:
            if order.order_id == order_id and order.status == "pending":
                order.status = "cancelled"
                return True
        return False

    def cancel_all_orders(self) -> int:
        """Cancel all pending orders. Returns count cancelled."""
        count = 0
        for order in self._orders:
            if order.status == "pending":
                order.status = "cancelled"
                count += 1
        return count

    def get_positions(self) -> list[AlpacaPosition]:
        """Get all open positions."""
        return list(self._positions.values())

    def get_position(self, symbol: str) -> AlpacaPosition | None:
        """Get a specific position."""
        return self._positions.get(symbol)

    def get_order_history(self, limit: int = 50) -> list[AlpacaOrder]:
        """Get recent order history."""
        return sorted(
            self._orders,
            key=lambda o: o.filled_at or datetime.min,
            reverse=True,
        )[:limit]

    def _update_position_from_fill(
        self,
        symbol: str,
        side: str,
        quantity: int,
        price: Decimal,
    ) -> None:
        """Update internal position tracking from a fill."""
        if side == "sell_to_open":
            # Opening a short position (premium received)
            premium = price * quantity * 100
            self._account.cash += premium
            self._account.buying_power -= price * quantity * 100 * 10  # rough margin

            existing = self._positions.get(symbol)
            if existing:
                existing.quantity += quantity
            else:
                self._positions[symbol] = AlpacaPosition(
                    symbol=symbol,
                    quantity=quantity,
                    avg_entry_price=price,
                    current_price=price,
                    unrealized_pnl=Decimal("0"),
                    market_value=premium,
                )

        elif side == "buy_to_close":
            # Closing a short position (paying to close)
            cost = price * quantity * 100
            self._account.cash -= cost

            existing = self._positions.get(symbol)
            if existing:
                existing.quantity -= quantity
                if existing.quantity <= 0:
                    del self._positions[symbol]

    def sync_with_paper_trader(self) -> dict[str, Any]:
        """Sync state with the paper trader for dashboard generation."""
        account = self.get_account()
        return {
            "equity": account.equity,
            "buying_power": account.buying_power,
            "cash": account.cash,
            "open_positions": len(account.positions),
            "pending_orders": len(account.open_orders),
        }
