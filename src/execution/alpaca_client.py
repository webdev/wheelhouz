"""Alpaca paper trading client — real SDK or in-memory fallback.

Uses alpaca-py for paper trading when API keys are set.
Falls back to in-memory simulation when keys are missing.
Base URL: https://paper-api.alpaca.markets
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import structlog

log = structlog.get_logger()


@dataclass
class AlpacaConfig:
    """Alpaca API configuration — reads from environment."""
    api_key: str = ""
    api_secret: str = ""
    base_url: str = "https://paper-api.alpaca.markets"
    paper: bool = True

    @classmethod
    def from_env(cls) -> AlpacaConfig:
        """Load config from ALPACA_API_KEY / ALPACA_API_SECRET env vars."""
        return cls(
            api_key=os.environ.get("ALPACA_API_KEY", ""),
            api_secret=os.environ.get("ALPACA_API_SECRET", ""),
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key and self.api_secret)


@dataclass
class AlpacaOrder:
    """An order submitted to Alpaca."""
    order_id: str
    symbol: str
    side: str  # "buy", "sell"
    position_intent: str  # "sell_to_open", "buy_to_close", etc.
    order_type: str  # "limit", "market"
    quantity: int
    limit_price: Decimal | None = None
    status: str = "pending"
    filled_price: Decimal | None = None
    filled_at: datetime | None = None
    time_in_force: str = "day"
    asset_class: str = "us_option"


@dataclass
class AlpacaPosition:
    """A position from Alpaca account."""
    symbol: str
    quantity: int
    avg_entry_price: Decimal
    current_price: Decimal
    unrealized_pnl: Decimal
    market_value: Decimal
    asset_class: str = "us_option"


@dataclass
class AlpacaAccountInfo:
    """Alpaca account summary."""
    equity: Decimal = Decimal("0")
    buying_power: Decimal = Decimal("0")
    cash: Decimal = Decimal("0")
    portfolio_value: Decimal = Decimal("0")
    positions: list[AlpacaPosition] = field(default_factory=list)
    open_orders: list[AlpacaOrder] = field(default_factory=list)


def build_option_symbol(
    underlying: str,
    expiration: date,
    option_type: str,
    strike: Decimal,
) -> str:
    """Build OCC option symbol: AAPL260515P00200000.

    Format: SYMBOL(6) + YYMMDD + C/P + Strike*1000 (8 digits).
    """
    sym = underlying.ljust(6)[:6]
    exp = expiration.strftime("%y%m%d")
    cp = "C" if option_type.lower().startswith("c") else "P"
    strike_int = int(strike * 1000)
    return f"{sym}{exp}{cp}{strike_int:08d}"


class AlpacaPaperClient:
    """Client for Alpaca paper trading — real SDK or in-memory fallback."""

    def __init__(self, config: AlpacaConfig | None = None) -> None:
        self.config = config or AlpacaConfig.from_env()
        self._sdk_client: Any = None
        self._fallback = not self.config.is_configured

        if self.config.is_configured:
            try:
                from alpaca.trading.client import TradingClient
                self._sdk_client = TradingClient(
                    api_key=self.config.api_key,
                    secret_key=self.config.api_secret,
                    paper=True,
                )
                log.info("alpaca_connected", mode="paper")
            except Exception as e:
                log.warning("alpaca_sdk_init_failed", error=str(e))
                self._fallback = True
        else:
            log.info("alpaca_fallback", reason="no API keys — using in-memory simulation")

        # In-memory state for fallback mode
        self._mem_orders: list[AlpacaOrder] = []
        self._mem_positions: dict[str, AlpacaPosition] = {}
        self._mem_cash = Decimal("100000")
        self._mem_equity = Decimal("100000")
        self._next_id = 1

    @property
    def is_live(self) -> bool:
        """True if connected to real Alpaca API, False if in-memory."""
        return not self._fallback

    # ── Account ────────────────────────────────────────────────

    def get_account(self) -> AlpacaAccountInfo:
        """Get current account state."""
        if not self._fallback:
            return self._sdk_get_account()
        return self._mem_get_account()

    def _sdk_get_account(self) -> AlpacaAccountInfo:
        acct = self._sdk_client.get_account()
        positions = self._sdk_get_positions()
        return AlpacaAccountInfo(
            equity=Decimal(str(acct.equity)),
            buying_power=Decimal(str(acct.buying_power)),
            cash=Decimal(str(acct.cash)),
            portfolio_value=Decimal(str(acct.portfolio_value)),
            positions=positions,
        )

    def _mem_get_account(self) -> AlpacaAccountInfo:
        return AlpacaAccountInfo(
            equity=self._mem_equity,
            buying_power=self._mem_cash,
            cash=self._mem_cash,
            portfolio_value=self._mem_equity,
            positions=list(self._mem_positions.values()),
            open_orders=[o for o in self._mem_orders if o.status == "pending"],
        )

    # ── Positions ──────────────────────────────────────────────

    def get_positions(self) -> list[AlpacaPosition]:
        """Get all open positions."""
        if not self._fallback:
            return self._sdk_get_positions()
        return list(self._mem_positions.values())

    def _sdk_get_positions(self) -> list[AlpacaPosition]:
        raw = self._sdk_client.get_all_positions()
        return [
            AlpacaPosition(
                symbol=p.symbol,
                quantity=int(p.qty),
                avg_entry_price=Decimal(str(p.avg_entry_price)),
                current_price=Decimal(str(p.current_price)),
                unrealized_pnl=Decimal(str(p.unrealized_pl)),
                market_value=Decimal(str(p.market_value)),
                asset_class=str(p.asset_class),
            )
            for p in raw
        ]

    # ── Orders ─────────────────────────────────────────────────

    def sell_to_open_option(
        self,
        underlying: str,
        expiration: date,
        option_type: str,
        strike: Decimal,
        quantity: int,
        limit_price: Decimal,
    ) -> AlpacaOrder:
        """Sell to open an option contract (CSP or CC)."""
        occ_symbol = build_option_symbol(underlying, expiration, option_type, strike)

        if not self._fallback:
            return self._sdk_submit_option_order(
                occ_symbol=occ_symbol,
                side="sell",
                position_intent="sell_to_open",
                quantity=quantity,
                limit_price=limit_price,
            )
        return self._mem_submit_order(
            symbol=occ_symbol,
            side="sell",
            position_intent="sell_to_open",
            quantity=quantity,
            limit_price=limit_price,
        )

    def buy_to_close_option(
        self,
        underlying: str,
        expiration: date,
        option_type: str,
        strike: Decimal,
        quantity: int,
        limit_price: Decimal,
    ) -> AlpacaOrder:
        """Buy to close an option position."""
        occ_symbol = build_option_symbol(underlying, expiration, option_type, strike)

        if not self._fallback:
            return self._sdk_submit_option_order(
                occ_symbol=occ_symbol,
                side="buy",
                position_intent="buy_to_close",
                quantity=quantity,
                limit_price=limit_price,
            )
        return self._mem_submit_order(
            symbol=occ_symbol,
            side="buy",
            position_intent="buy_to_close",
            quantity=quantity,
            limit_price=limit_price,
        )

    def _sdk_submit_option_order(
        self,
        occ_symbol: str,
        side: str,
        position_intent: str,
        quantity: int,
        limit_price: Decimal,
    ) -> AlpacaOrder:
        from alpaca.trading.enums import (
            OrderSide,
            OrderType,
            PositionIntent,
            TimeInForce,
        )
        from alpaca.trading.requests import LimitOrderRequest

        intent_map = {
            "sell_to_open": PositionIntent.SELL_TO_OPEN,
            "buy_to_close": PositionIntent.BUY_TO_CLOSE,
            "buy_to_open": PositionIntent.BUY_TO_OPEN,
            "sell_to_close": PositionIntent.SELL_TO_CLOSE,
        }

        req = LimitOrderRequest(
            symbol=occ_symbol,
            qty=float(quantity),
            side=OrderSide.SELL if side == "sell" else OrderSide.BUY,
            type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            limit_price=float(limit_price),
            position_intent=intent_map[position_intent],
        )

        result = self._sdk_client.submit_order(req)

        return AlpacaOrder(
            order_id=str(result.id),
            symbol=occ_symbol,
            side=side,
            position_intent=position_intent,
            order_type="limit",
            quantity=quantity,
            limit_price=limit_price,
            status=str(result.status.value) if result.status else "new",
            filled_price=(
                Decimal(str(result.filled_avg_price))
                if result.filled_avg_price else None
            ),
        )

    def _mem_submit_order(
        self,
        symbol: str,
        side: str,
        position_intent: str,
        quantity: int,
        limit_price: Decimal,
    ) -> AlpacaOrder:
        """In-memory order simulation with immediate fill."""
        order_id = f"SIM-{self._next_id:06d}"
        self._next_id += 1

        order = AlpacaOrder(
            order_id=order_id,
            symbol=symbol,
            side=side,
            position_intent=position_intent,
            order_type="limit",
            quantity=quantity,
            limit_price=limit_price,
            status="filled",
            filled_price=limit_price,
            filled_at=datetime.now(timezone.utc),
        )

        # Update in-memory state
        if position_intent == "sell_to_open":
            premium = limit_price * quantity * 100
            self._mem_cash += premium
            self._mem_equity += premium
            existing = self._mem_positions.get(symbol)
            if existing:
                existing.quantity += quantity
            else:
                self._mem_positions[symbol] = AlpacaPosition(
                    symbol=symbol,
                    quantity=quantity,
                    avg_entry_price=limit_price,
                    current_price=limit_price,
                    unrealized_pnl=Decimal("0"),
                    market_value=limit_price * quantity * 100,
                )

        elif position_intent == "buy_to_close":
            cost = limit_price * quantity * 100
            self._mem_cash -= cost
            existing = self._mem_positions.get(symbol)
            if existing:
                pnl = (existing.avg_entry_price - limit_price) * quantity * 100
                self._mem_equity += pnl
                existing.quantity -= quantity
                if existing.quantity <= 0:
                    del self._mem_positions[symbol]

        self._mem_orders.append(order)
        log.info("sim_order_filled", order_id=order_id, symbol=symbol,
                 intent=position_intent, qty=quantity, price=str(limit_price))
        return order

    # ── Order management ───────────────────────────────────────

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order."""
        if not self._fallback:
            try:
                self._sdk_client.cancel_order_by_id(order_id)
                return True
            except Exception:
                return False

        for order in self._mem_orders:
            if order.order_id == order_id and order.status == "pending":
                order.status = "cancelled"
                return True
        return False

    def cancel_all_orders(self) -> int:
        """Cancel all pending orders."""
        if not self._fallback:
            try:
                self._sdk_client.cancel_orders()
                return 0  # SDK doesn't return count
            except Exception:
                return 0

        count = 0
        for order in self._mem_orders:
            if order.status == "pending":
                order.status = "cancelled"
                count += 1
        return count

    def get_order_history(self, limit: int = 50) -> list[AlpacaOrder]:
        """Get recent order history."""
        if not self._fallback:
            from alpaca.trading.requests import GetOrdersRequest
            req = GetOrdersRequest(limit=limit)
            raw = self._sdk_client.get_orders(req)
            return [
                AlpacaOrder(
                    order_id=str(o.id),
                    symbol=str(o.symbol),
                    side=str(o.side.value) if o.side else "",
                    position_intent="",
                    order_type=str(o.order_type.value) if o.order_type else "",
                    quantity=int(o.qty) if o.qty else 0,
                    limit_price=(
                        Decimal(str(o.limit_price)) if o.limit_price else None
                    ),
                    status=str(o.status.value) if o.status else "",
                    filled_price=(
                        Decimal(str(o.filled_avg_price))
                        if o.filled_avg_price else None
                    ),
                )
                for o in raw
            ]

        return sorted(
            self._mem_orders,
            key=lambda o: o.filled_at or datetime.min,
            reverse=True,
        )[:limit]

    # ── Option chain lookup ────────────────────────────────────

    def get_option_contracts(
        self,
        underlying: str,
        expiration_gte: date | None = None,
        expiration_lte: date | None = None,
        strike_gte: Decimal | None = None,
        strike_lte: Decimal | None = None,
        option_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Look up available option contracts from Alpaca."""
        if self._fallback:
            return []

        from alpaca.trading.enums import ContractType
        from alpaca.trading.requests import GetOptionContractsRequest

        req = GetOptionContractsRequest(
            underlying_symbols=[underlying],
            expiration_date_gte=expiration_gte.isoformat() if expiration_gte else None,
            expiration_date_lte=expiration_lte.isoformat() if expiration_lte else None,
            strike_price_gte=str(strike_gte) if strike_gte else None,
            strike_price_lte=str(strike_lte) if strike_lte else None,
            type=(
                ContractType.PUT if option_type == "put"
                else ContractType.CALL if option_type == "call"
                else None
            ),
            limit=100,
        )

        raw = self._sdk_client.get_option_contracts(req)
        return [
            {
                "symbol": c.symbol,
                "underlying": c.underlying_symbol,
                "expiration": c.expiration_date,
                "strike": Decimal(str(c.strike_price)),
                "type": c.type,
                "status": c.status,
            }
            for c in (raw.option_contracts if raw.option_contracts else [])
        ]

    # ── Clock / calendar ───────────────────────────────────────

    def is_market_open(self) -> bool:
        """Check if market is currently open."""
        if not self._fallback:
            try:
                clock = self._sdk_client.get_clock()
                return bool(clock.is_open)
            except Exception:
                return False
        return False  # can't know without API
