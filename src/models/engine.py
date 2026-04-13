"""Three-engine portfolio allocation models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from src.models.position import Position


@dataclass
class CorePosition:
    """A long-term holding in Engine 1."""
    symbol: str
    shares: int
    cost_basis: Decimal
    current_value: Decimal
    holding_period_days: int
    is_ltcg_eligible: bool
    days_until_ltcg: int | None = None
    conviction: str = "medium"
    thesis: str = ""

    # Coverage
    total_shares: int = 0
    covered_shares: int = 0
    uncovered_shares: int = 0
    coverage_ratio: float = 0.0

    # Dividend
    annual_dividend: Decimal | None = None
    next_ex_div: date | None = None
    dividend_yield: float | None = None


@dataclass
class EngineAllocation:
    """Current state of the three portfolio engines."""
    # Engine 1: Core Holdings
    core_holdings: dict[str, CorePosition] = field(default_factory=dict)
    core_target_pct: float = 0.45
    core_actual_pct: float = 0.0
    core_expected_return: float = 0.18

    # Engine 2: Active Wheel
    wheel_positions: dict[str, list[Position]] = field(default_factory=dict)
    wheel_target_pct: float = 0.45
    wheel_actual_pct: float = 0.0
    wheel_expected_return: float = 0.45

    # Engine 3: Dry Powder
    cash_available: Decimal = Decimal("0")
    powder_target_pct: float = 0.10
    powder_actual_pct: float = 0.0


@dataclass
class RebalanceAction:
    """A single rebalancing action between engines."""
    action: str         # "shift_to_core", "shift_to_wheel", "build_powder", "deploy_powder"
    amount: Decimal
    from_engine: str
    to_engine: str
    reasoning: str
    urgency: str        # "now", "this_week", "next_rebalance"
