"""Loss management — per-trade and portfolio-level loss rules.

Decision tree for losing positions and portfolio-level circuit breakers.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.models.market import MarketContext
from src.models.position import Position


@dataclass
class LossManagementRules:
    """Configurable loss management thresholds."""
    # Per-trade max loss
    put_max_loss_multiplier: float = 2.0        # close when option = 2x premium
    weekly_put_max_loss_multiplier: float = 1.5  # tighter for DTE <= 10
    strangle_leg_max_loss_multiplier: float = 2.0

    # Underlying-based stop
    underlying_crash_stop_pct: float = 0.15  # underlying 15% below strike

    # Portfolio-level limits
    weekly_loss_limit_pct: float = 0.03      # 3% NLV in a week -> 2-day pause
    weekly_loss_cooldown_days: int = 2
    monthly_loss_limit_pct: float = 0.08     # 8% NLV in a month -> DEFEND
    consecutive_loss_size_reduction: int = 3  # 3 losers -> 50% size
    size_reduction_factor: float = 0.50
    size_reduction_trades: int = 5


def evaluate_losing_position(
    pos: Position,
    mkt: MarketContext,
    rules: LossManagementRules | None = None,
) -> tuple[str, str]:
    """Evaluate a losing position and recommend action.

    Returns (action, reasoning) where action is one of:
    "CLOSE_LOSS", "ROLL", "TAKE_ASSIGNMENT", "HOLD"
    """
    r = rules or LossManagementRules()

    # Determine loss multiple
    if pos.entry_price <= 0:
        return ("HOLD", "No entry price data")

    loss_multiple = float(pos.current_price / pos.entry_price)
    is_weekly = pos.days_to_expiry <= 10
    max_mult = r.weekly_put_max_loss_multiplier if is_weekly else r.put_max_loss_multiplier
    is_itm = pos.distance_from_strike_pct < 0

    # Check underlying crash stop
    if pos.strike > 0:
        underlying_drop = float(
            (pos.underlying_price - pos.strike) / pos.strike
        )
        if underlying_drop < -r.underlying_crash_stop_pct:
            return (
                "CLOSE_LOSS",
                f"Underlying crashed {underlying_drop:.1%} below strike. "
                f"Emergency close.",
            )

    # Loss stop triggered
    if loss_multiple >= max_mult:
        return (
            "CLOSE_LOSS",
            f"Loss stop: option at {loss_multiple:.1f}x entry "
            f"(max {max_mult}x). Close immediately.",
        )

    # Approaching loss stop (warning zone at 1.7x)
    if loss_multiple >= 1.7:
        return (
            "HOLD",
            f"Approaching loss stop: {loss_multiple:.1f}x entry. "
            f"Monitor closely.",
        )

    # ITM but under loss stop — try to roll
    if is_itm and loss_multiple < max_mult:
        if pos.days_to_expiry > 21:
            return (
                "ROLL",
                f"ITM ({pos.distance_from_strike_pct:+.1f}% from strike) "
                f"with {pos.days_to_expiry} DTE. Roll down and out for credit.",
            )
        else:
            return (
                "TAKE_ASSIGNMENT",
                f"ITM near expiry ({pos.days_to_expiry} DTE). "
                f"Take assignment and sell covered calls.",
            )

    # OTM, within range — let theta work
    return (
        "HOLD",
        f"OTM ({pos.distance_from_strike_pct:+.1f}% from strike), "
        f"loss {loss_multiple:.1f}x entry. Theta is working.",
    )
