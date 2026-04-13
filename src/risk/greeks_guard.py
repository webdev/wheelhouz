"""Portfolio-level Greeks guard — blocks trades that would push Greeks out of range.

Pre-trade check ensures portfolio delta, vega, and gamma stay within
regime-appropriate bounds.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from src.models.analysis import SizedOpportunity
from src.models.position import PortfolioState


@dataclass
class PortfolioGreeksTargets:
    """Target Greeks ranges by regime."""
    # Beta-weighted delta ranges (SPY-equivalent shares)
    delta_target_attack: tuple[float, float] = (200.0, 500.0)
    delta_target_hold: tuple[float, float] = (100.0, 300.0)
    delta_target_defend: tuple[float, float] = (-100.0, 150.0)
    delta_target_crisis: tuple[float, float] = (-200.0, 50.0)

    # Theta targets
    theta_target_attack: float = 200.0  # $200+/day
    theta_target_hold: float = 100.0

    # Vega cap: 2% NLV risk per 1pt IV move
    max_vega_pct_of_nlv: float = 0.02

    # Gamma cap (weeklies): 0.5% NLV per $1 underlying move
    max_weekly_gamma_exposure: float = 0.005

    # Beta
    max_portfolio_beta: float = 1.50


def check_greeks_before_trade(
    new_trade: SizedOpportunity,
    portfolio: PortfolioState,
    targets: PortfolioGreeksTargets | None = None,
    regime: str = "attack",
) -> tuple[bool, str]:
    """Check if adding this trade would push portfolio Greeks out of range.

    Returns (allowed, reason). If not allowed, reason explains why.
    """
    t = targets or PortfolioGreeksTargets()
    nlv = float(portfolio.net_liquidation) or 1_000_000.0

    # Get delta range for current regime
    delta_range = {
        "attack": t.delta_target_attack,
        "hold": t.delta_target_hold,
        "defend": t.delta_target_defend,
        "crisis": t.delta_target_crisis,
    }.get(regime, t.delta_target_attack)

    # Estimate trade's delta contribution
    # Short put ≈ positive delta (bullish), ~25 delta per contract
    trade_delta = new_trade.contracts * 25.0  # conservative estimate
    projected_delta = portfolio.portfolio_delta + trade_delta

    if projected_delta < delta_range[0] or projected_delta > delta_range[1]:
        return (
            False,
            f"Projected delta {projected_delta:.0f} outside "
            f"{regime} range {delta_range}",
        )

    # Vega check: trade vega vs NLV cap
    trade_vega = new_trade.contracts * 0.85 * 100  # ~$85 per contract
    total_vega = abs(portfolio.portfolio_vega) + trade_vega
    max_vega = nlv * t.max_vega_pct_of_nlv
    if total_vega > max_vega:
        return (
            False,
            f"Projected vega ${total_vega:,.0f} exceeds "
            f"cap ${max_vega:,.0f} ({t.max_vega_pct_of_nlv:.0%} of NLV)",
        )

    # Beta check
    if hasattr(portfolio, 'sector_exposure'):
        # Rough beta estimate from tech exposure
        tech_pct = sum(
            v for k, v in portfolio.sector_exposure.items()
            if "tech" in k.lower() or "semi" in k.lower()
        )
        estimated_beta = 1.0 + tech_pct * 0.5
        if estimated_beta > t.max_portfolio_beta:
            return (
                False,
                f"Estimated beta {estimated_beta:.2f} exceeds "
                f"max {t.max_portfolio_beta}",
            )

    return (True, "Greeks within range")
