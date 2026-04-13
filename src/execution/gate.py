"""Live-price gate validation.

No fixed timers. User taps EXECUTE, system validates in 2 seconds.
ALL 6 checks must pass. Any failure rejects with explanation.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from src.models.execution import GateValidation, LivePriceGate


def validate_gate(
    gate: LivePriceGate,
    live_price: Decimal,
    live_premium: Decimal,
    live_iv_rank: float,
    live_delta: float,
    live_bid: Decimal,
    live_ask: Decimal,
    disqualifying_events: list[str] | None = None,
    market_open: bool = True,
) -> GateValidation:
    """Validate all gate conditions against live market data.

    Returns GateValidation with pass/fail for each check.
    ALL must pass for is_valid=True.
    """
    checks_passed: list[str] = []
    checks_failed: list[str] = []

    # 1. Underlying within ±3% of analysis price
    pct_move = abs(float(
        (live_price - gate.analysis_price) / gate.analysis_price
    )) * 100
    if gate.underlying_floor <= live_price <= gate.underlying_ceiling:
        checks_passed.append(
            f"Underlying ${live_price} within range "
            f"${gate.underlying_floor}-${gate.underlying_ceiling}"
        )
    else:
        checks_failed.append(
            f"Underlying ${live_price} outside range "
            f"${gate.underlying_floor}-${gate.underlying_ceiling} "
            f"({pct_move:.1f}% from analysis)"
        )

    # 2. Premium >= 80% of analysis premium
    min_premium = gate.min_premium
    if live_premium >= min_premium:
        checks_passed.append(
            f"Premium ${live_premium} >= min ${min_premium}"
        )
    else:
        checks_failed.append(
            f"Premium ${live_premium} < min ${min_premium} "
            f"(analysis was ${gate.analysis_premium})"
        )

    # 3. IV rank above minimum
    if live_iv_rank >= gate.min_iv_rank:
        checks_passed.append(
            f"IV rank {live_iv_rank:.0f} >= {gate.min_iv_rank:.0f}"
        )
    else:
        checks_failed.append(
            f"IV rank {live_iv_rank:.0f} < min {gate.min_iv_rank:.0f}"
        )

    # 4. Delta within range
    if abs(live_delta) <= gate.max_abs_delta:
        checks_passed.append(
            f"Delta {live_delta:.2f} within ±{gate.max_abs_delta}"
        )
    else:
        checks_failed.append(
            f"Delta {live_delta:.2f} exceeds max ±{gate.max_abs_delta}"
        )

    # 5. No disqualifying events
    events = disqualifying_events or gate.disqualifying_events
    if not events:
        checks_passed.append("No disqualifying events")
    else:
        checks_failed.append(f"Disqualifying events: {', '.join(events)}")

    # 6. Bid-ask spread < 15% of premium
    spread = live_ask - live_bid
    mid = (live_ask + live_bid) / 2
    if mid > 0:
        spread_pct = float(spread / mid)
        if spread_pct < 0.15:
            checks_passed.append(
                f"Spread ${spread:.2f} ({spread_pct:.1%} of mid)"
            )
        else:
            checks_failed.append(
                f"Spread ${spread:.2f} ({spread_pct:.1%}) >= 15% of mid"
            )
    else:
        checks_failed.append("No valid bid-ask data")

    # Market must be open (optional)
    if gate.market_must_be_open and not market_open:
        checks_failed.append("Market is closed")

    # Gate age check
    age_hours = (datetime.utcnow() - gate.analysis_time).total_seconds() / 3600
    if age_hours > gate.max_age_hours:
        checks_failed.append(
            f"Gate is {age_hours:.1f}h old (max {gate.max_age_hours}h)"
        )

    is_valid = len(checks_failed) == 0
    reason = checks_failed[0] if checks_failed else "All checks passed"

    return GateValidation(
        is_valid=is_valid,
        reason=reason,
        checks_passed=checks_passed,
        checks_failed=checks_failed,
        live_price=live_price,
        live_premium=live_premium,
        live_iv_rank=live_iv_rank,
        live_delta=live_delta,
    )
