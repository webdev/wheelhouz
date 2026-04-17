"""Analysis engine models: strikes, sizing, risk reports."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from src.models.signals import AlphaSignal


@dataclass
class SmartStrike:
    """A strike selected at a technically meaningful level."""
    strike: Decimal
    delta: float
    premium: Decimal
    yield_on_capital: float
    annualized_yield: float
    technical_reason: str | None = None
    strike_score: float = 0.0
    expiration: date | None = None


@dataclass
class Opportunity:
    """A raw trade opportunity before sizing."""
    symbol: str
    trade_type: str         # "sell_put", "sell_call"
    strike: Decimal
    expiration: date
    premium: Decimal
    yield_on_capital: float
    annualized_yield: float
    iv_rank: float
    delta: float
    smart_strike: SmartStrike
    signals: list[AlphaSignal] = field(default_factory=list)


@dataclass
class SizedOpportunity:
    """A fully sized trade recommendation ready for execution."""
    symbol: str
    trade_type: str
    strike: Decimal
    expiration: date | None
    premium: Decimal
    contracts: int
    capital_deployed: Decimal
    portfolio_pct: float
    yield_on_capital: float
    annualized_yield: float
    conviction: str          # "high", "medium", "low"
    signals: list[AlphaSignal] = field(default_factory=list)
    smart_strike: SmartStrike | None = None
    reasoning: str = ""
    conviction_label: str | None = None


@dataclass
class RiskReport:
    """Portfolio risk assessment."""
    # Concentration
    adbe_pct: float
    top_5_concentration: float
    sector_breakdown: dict[str, float] = field(default_factory=dict)

    # Greeks summary
    portfolio_delta_dollars: Decimal = Decimal("0")
    daily_theta: Decimal = Decimal("0")
    portfolio_beta: float = 0.0

    # Stress scenarios
    impact_5pct_down: Decimal = Decimal("0")
    impact_10pct_down: Decimal = Decimal("0")
    impact_iv_spike_20pct: Decimal = Decimal("0")

    # Alerts
    concentration_warnings: list[str] = field(default_factory=list)
    margin_utilization: float = 0.0

    # Aggressive metrics
    capital_efficiency: float = 0.0
    idle_capital_pct: float = 0.0
    days_since_last_trade: int = 0
