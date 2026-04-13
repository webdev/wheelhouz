"""Drawdown decomposition — diagnose the cause of portfolio drawdowns.

Runs after any drawdown exceeding 5%. Determines whether the cause was
correlation, volatility, a single blowup, or bad signals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass
class DrawdownDecomposition:
    """Detailed breakdown of a portfolio drawdown period."""
    period_start: date
    period_end: date
    total_drawdown_pct: float
    total_drawdown_dollars: Decimal

    # 1. Individual blowups
    single_position_losses: list[dict[str, object]] = field(default_factory=list)
    largest_single_loss: dict[str, object] = field(default_factory=dict)
    single_position_contribution: float = 0.0

    # 2. Correlation drag
    market_move_pct: float = 0.0
    beta_explained_loss: Decimal = Decimal("0")
    correlation_contribution: float = 0.0

    # 3. Volatility regime error
    iv_move_pct: float = 0.0
    vega_explained_loss: Decimal = Decimal("0")
    vega_contribution: float = 0.0

    # 4. Strategy breakdown
    strategy_losses: dict[str, Decimal] = field(default_factory=dict)
    worst_strategy: str = ""

    # 5. Signal quality
    losses_from_high_conviction: Decimal = Decimal("0")
    losses_from_low_conviction: Decimal = Decimal("0")
    signal_types_that_failed: list[str] = field(default_factory=list)

    # Diagnosis
    primary_cause: str = ""
    recommended_action: str = ""


def decompose_drawdown(
    losing_trades: list[dict[str, object]],
    peak_nlv: Decimal,
    trough_nlv: Decimal,
    spy_move_pct: float,
    vix_change: float,
    portfolio_beta: float,
    portfolio_vega: float,
) -> DrawdownDecomposition:
    """Decompose a drawdown into contributing factors.

    Called when drawdown exceeds 5% from peak.
    """
    total_dd_pct = float((peak_nlv - trough_nlv) / peak_nlv) if peak_nlv > 0 else 0.0
    total_dd_dollars = peak_nlv - trough_nlv

    dd = DrawdownDecomposition(
        period_start=date.today(),
        period_end=date.today(),
        total_drawdown_pct=total_dd_pct,
        total_drawdown_dollars=total_dd_dollars,
    )

    if total_dd_dollars == 0:
        return dd

    # Beta-explained loss
    dd.market_move_pct = spy_move_pct
    dd.beta_explained_loss = Decimal(str(
        abs(spy_move_pct) * portfolio_beta * float(peak_nlv)
    ))
    dd.correlation_contribution = min(
        1.0, float(dd.beta_explained_loss / total_dd_dollars),
    )

    # Vega-explained loss
    dd.iv_move_pct = vix_change
    dd.vega_explained_loss = Decimal(str(abs(vix_change * portfolio_vega)))
    dd.vega_contribution = min(
        1.0, float(dd.vega_explained_loss / total_dd_dollars),
    )

    # Single position losses
    for trade in losing_trades:
        loss = Decimal(str(trade.get("loss_dollars", 0)))
        dd.single_position_losses.append(trade)
        if not dd.largest_single_loss or loss > Decimal(str(
            dd.largest_single_loss.get("loss_dollars", 0)
        )):
            dd.largest_single_loss = trade

    if dd.largest_single_loss:
        largest = Decimal(str(dd.largest_single_loss.get("loss_dollars", 0)))
        dd.single_position_contribution = float(largest / total_dd_dollars)

    # Determine primary cause
    contributions = {
        "single_blowup": dd.single_position_contribution,
        "correlation": dd.correlation_contribution,
        "vega": dd.vega_contribution,
    }
    dd.primary_cause = max(contributions, key=contributions.get)  # type: ignore[arg-type]

    # Recommended actions
    actions = {
        "single_blowup": (
            "Reduce max position size. Tighten loss stops. "
            "Review the signal that generated this trade."
        ),
        "correlation": (
            "Add hedges (SPY puts). Reduce correlated cluster. "
            "Target portfolio beta <= 1.20."
        ),
        "vega": (
            "Reduce short option count in high-VIX environment. "
            "Consider protective VIX calls."
        ),
    }
    dd.recommended_action = actions.get(dd.primary_cause, "Review portfolio")

    return dd


def format_drawdown_report(dd: DrawdownDecomposition) -> str:
    """Format drawdown decomposition for Telegram."""
    return (
        f"DRAWDOWN ANALYSIS\n"
        f"Total: {dd.total_drawdown_pct:.1%} "
        f"(${dd.total_drawdown_dollars:,.0f})\n\n"
        f"DECOMPOSITION:\n"
        f"  Market correlation: {dd.correlation_contribution:.0%} "
        f"(SPY {dd.market_move_pct:+.1f}%)\n"
        f"  IV spike: {dd.vega_contribution:.0%} "
        f"(VIX {dd.iv_move_pct:+.1f})\n"
        f"  Single blowup: {dd.single_position_contribution:.0%}\n\n"
        f"PRIMARY CAUSE: {dd.primary_cause.replace('_', ' ').title()}\n"
        f"ACTION: {dd.recommended_action}"
    )
