"""Day-1 onboarding models: portfolio intake, gap analysis, transition plan."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from src.models.position import Position
from src.models.tax import TaxContext


@dataclass
class StockClassification:
    """A stock position pending engine classification by the user."""
    symbol: str
    shares: int
    cost_basis: Decimal
    current_price: Decimal
    unrealized_pnl: Decimal
    holding_period_days: int
    is_ltcg: bool

    # Human input (via Telegram onboarding flow)
    engine: str | None = None       # "engine1" or "engine2"
    conviction: str | None = None   # "high", "medium", "low"

    # System-suggested classification
    suggested_engine: str | None = None
    suggestion_reason: str | None = None


@dataclass
class PortfolioIntake:
    """Complete portfolio snapshot from broker, classified for onboarding."""
    short_puts: list[Position] = field(default_factory=list)
    short_calls: list[Position] = field(default_factory=list)
    cash: Decimal = Decimal("0")
    stock_positions: list[StockClassification] = field(default_factory=list)
    rsu_espp_positions: list[Position] = field(default_factory=list)
    positions_with_gains: list[TaxContext] = field(default_factory=list)
    positions_with_losses: list[TaxContext] = field(default_factory=list)
    wash_sale_risk_tickers: list[str] = field(default_factory=list)


@dataclass
class GapAnalysis:
    """Comparison of current portfolio to target three-engine allocation."""
    # Current vs target allocation
    engine1_current_pct: float = 0.0
    engine1_target_pct: float = 0.45
    engine1_gap: float = 0.0

    engine2_current_pct: float = 0.0
    engine2_target_pct: float = 0.45
    engine2_gap: float = 0.0

    engine3_current_pct: float = 0.0
    engine3_target_pct: float = 0.10
    engine3_gap: float = 0.0

    # Issues by urgency
    critical_issues: list[str] = field(default_factory=list)
    important_issues: list[str] = field(default_factory=list)
    optimization_issues: list[str] = field(default_factory=list)

    # Position-level issues
    stranded_profits: list[dict[str, object]] = field(default_factory=list)
    earnings_conflicts: list[dict[str, object]] = field(default_factory=list)
    concentration_violations: list[dict[str, object]] = field(default_factory=list)
    tax_traps: list[dict[str, object]] = field(default_factory=list)
    missing_hedges: list[dict[str, object]] = field(default_factory=list)
    uncovered_stock: list[dict[str, object]] = field(default_factory=list)

    # Greeks assessment
    current_delta: float = 0.0
    target_delta_range: tuple[float, float] = (200.0, 500.0)
    current_theta: float = 0.0
    estimated_target_theta: float = 0.0


@dataclass
class TransitionAction:
    """A single action in the portfolio transition plan."""
    urgency: str            # "immediate", "short_term", "medium_term"
    action: str             # "close", "hold", "sell_calls", "reclassify", "buy_hedge"
    symbol: str
    description: str
    tax_impact: Decimal
    opportunity_cost: Decimal
    rationale: str


@dataclass
class TransitionPlan:
    """Phased migration from current portfolio to three-engine model."""
    immediate_actions: list[TransitionAction] = field(default_factory=list)
    short_term_actions: list[TransitionAction] = field(default_factory=list)
    medium_term_actions: list[TransitionAction] = field(default_factory=list)

    projected_engine1_pct: float = 0.45
    projected_engine2_pct: float = 0.45
    projected_engine3_pct: float = 0.10
    projected_daily_theta: Decimal = Decimal("0")
    estimated_transition_weeks: int = 8
