"""Position and portfolio state models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal


@dataclass
class Position:
    """A single portfolio position (option or stock)."""
    symbol: str
    position_type: str          # "short_put", "short_call", "long_stock"
    quantity: int
    strike: Decimal
    expiration: date | None
    entry_price: Decimal        # premium received (options) or purchase price (stock)
    current_price: Decimal      # current option mark or stock price
    underlying_price: Decimal
    cost_basis: Decimal         # for stock: assignment cost basis

    # Greeks (from broker or calculated)
    delta: float
    theta: float
    gamma: float
    vega: float
    iv: float                   # implied volatility of the specific contract

    # Derived
    days_to_expiry: int = 0
    distance_from_strike_pct: float = 0.0
    profit_pct: float = 0.0
    max_profit: Decimal = Decimal("0")
    max_loss: Decimal = Decimal("0")

    # Metadata
    account_id: str = ""
    engine: str = ""            # "engine1", "engine2", "engine3"
    option_type: str = ""       # "put" or "call"
    capital_at_risk: Decimal = Decimal("0")
    current_profit: Decimal = Decimal("0")
    purchase_date: date = field(default_factory=date.today)
    holding_period_days: int = 0
    unrealized_pnl: Decimal = Decimal("0")
    market_value: Decimal = Decimal("0")


@dataclass
class PortfolioState:
    """Aggregate portfolio state across all accounts."""
    positions: list[Position] = field(default_factory=list)
    cash_available: Decimal = Decimal("0")
    buying_power: Decimal = Decimal("0")
    net_liquidation: Decimal = Decimal("0")
    portfolio_delta: float = 0.0
    portfolio_theta: float = 0.0
    portfolio_vega: float = 0.0
    concentration: dict[str, float] = field(default_factory=dict)
    sector_exposure: dict[str, float] = field(default_factory=dict)
    margin_utilization: float = 0.0
    timestamp: datetime = field(default_factory=datetime.utcnow)
