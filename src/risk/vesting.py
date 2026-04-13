"""ESPP/RSU vesting tracker — countdown, sell plan, ADBE concentration management.

Tracks upcoming vesting events and generates sell + redeploy plans
to keep employer stock concentration below target.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal


@dataclass
class VestingEvent:
    """An upcoming ESPP or RSU vesting event."""
    vest_date: date
    vest_type: str  # "espp", "rsu"
    estimated_shares: int
    estimated_price: Decimal
    estimated_value: Decimal

    @property
    def days_until_vest(self) -> int:
        return (self.vest_date - date.today()).days


@dataclass
class ConcentrationPlan:
    """Quarterly sell plan to reduce employer stock concentration."""
    current_shares: int
    current_price: Decimal
    current_value: Decimal
    current_pct: float  # of NLV
    target_pct: float
    shares_to_sell: int
    sell_value: Decimal
    quarterly_sell_shares: int
    quarters_to_target: int
    tax_impact: Decimal


@dataclass
class VestingTracker:
    """Tracks vesting events and generates sell plans."""
    symbol: str = "ADBE"
    target_concentration: float = 0.15
    upcoming_events: list[VestingEvent] = field(default_factory=list)

    def add_event(
        self,
        vest_date: date,
        vest_type: str,
        shares: int,
        estimated_price: Decimal,
    ) -> None:
        """Add a vesting event."""
        self.upcoming_events.append(VestingEvent(
            vest_date=vest_date,
            vest_type=vest_type,
            estimated_shares=shares,
            estimated_price=estimated_price,
            estimated_value=estimated_price * shares,
        ))

    def get_upcoming(self, days: int = 90) -> list[VestingEvent]:
        """Get vesting events in the next N days."""
        cutoff = date.today() + timedelta(days=days)
        return sorted(
            [e for e in self.upcoming_events if e.vest_date <= cutoff],
            key=lambda e: e.vest_date,
        )

    def generate_sell_plan(
        self,
        current_shares: int,
        current_price: Decimal,
        nlv: Decimal,
        stcg_rate: float = 0.408,
    ) -> ConcentrationPlan:
        """Generate a quarterly sell plan to reduce concentration.

        Calculates how many shares to sell per quarter to reach target.
        """
        current_value = current_price * current_shares
        current_pct = float(current_value / nlv) if nlv > 0 else 0.0

        target_value = nlv * Decimal(str(self.target_concentration))
        excess_value = max(Decimal("0"), current_value - target_value)
        shares_to_sell = int(excess_value / current_price) if current_price > 0 else 0

        # Spread over quarters (avoid dumping all at once)
        quarters = max(1, shares_to_sell // 50 + 1)  # ~50 shares/quarter
        quarterly = shares_to_sell // quarters if quarters > 0 else 0

        # Tax impact estimate (assume all STCG for conservative estimate)
        # Only taxed on gains, not full value — assume 50% gain ratio
        gain_ratio = Decimal("0.50")
        tax_impact = excess_value * gain_ratio * Decimal(str(stcg_rate))

        return ConcentrationPlan(
            current_shares=current_shares,
            current_price=current_price,
            current_value=current_value,
            current_pct=current_pct,
            target_pct=self.target_concentration,
            shares_to_sell=shares_to_sell,
            sell_value=excess_value,
            quarterly_sell_shares=quarterly,
            quarters_to_target=quarters,
            tax_impact=tax_impact.quantize(Decimal("1")),
        )


def check_employer_emergency(
    symbol: str,
    change_5d_pct: float,
    nlv_pct: float,
    target_pct: float = 0.15,
) -> tuple[bool, str]:
    """Check if employer stock requires emergency sell.

    Trigger: -20% in 5 days. Tax efficiency NEVER overrides employer risk.
    """
    if change_5d_pct <= -0.20:
        return (
            True,
            f"EMERGENCY: {symbol} down {change_5d_pct:.0%} in 5 days. "
            f"Sell 50% above {target_pct:.0%} target immediately. "
            f"Tax override: employer risk takes priority.",
        )
    return (False, f"{symbol} at {nlv_pct:.0%} NLV, {change_5d_pct:+.0%} 5d — no emergency.")


def format_vesting_summary(
    tracker: VestingTracker,
    plan: ConcentrationPlan | None = None,
) -> str:
    """Format vesting events and sell plan for briefing."""
    lines = [f"VESTING TRACKER: {tracker.symbol}"]

    upcoming = tracker.get_upcoming(90)
    if upcoming:
        lines.append("\nUPCOMING VESTS (90 days):")
        for e in upcoming:
            lines.append(
                f"  {e.vest_date}: {e.vest_type.upper()} — "
                f"{e.estimated_shares} shares "
                f"(~${e.estimated_value:,.0f}), "
                f"{e.days_until_vest} days"
            )
    else:
        lines.append("  No vesting events in next 90 days.")

    if plan:
        lines.append(f"\nCONCENTRATION PLAN:")
        lines.append(f"  Current: {plan.current_pct:.1%} of NLV "
                     f"(${plan.current_value:,.0f})")
        lines.append(f"  Target:  {plan.target_pct:.0%}")
        if plan.shares_to_sell > 0:
            lines.append(f"  Sell {plan.shares_to_sell} shares "
                        f"(${plan.sell_value:,.0f})")
            lines.append(f"  Quarterly pace: {plan.quarterly_sell_shares} shares "
                        f"over {plan.quarters_to_target} quarters")
            lines.append(f"  Est. tax impact: ${plan.tax_impact:,.0f}")
        else:
            lines.append("  Within target — no sales needed.")

    return "\n".join(lines)
