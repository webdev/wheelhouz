"""Order management — smart limit pricing and execution windows.

Options should NEVER use market orders. This module calculates
optimal limit prices and validates execution timing.
"""

from __future__ import annotations

from datetime import time
from decimal import Decimal, ROUND_HALF_UP

from src.models.paper import ExecutionRules


def calculate_smart_limit(
    bid: Decimal,
    ask: Decimal,
    direction: str,
) -> Decimal:
    """Calculate a smart limit price based on bid-ask.

    For sells: mid price minus 1 penny (improves fill speed).
    For buys: mid price plus 1 penny (buying to close).
    """
    mid = (bid + ask) / 2
    if direction == "sell":
        return (mid - Decimal("0.01")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP,
        )
    else:
        return (mid + Decimal("0.01")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP,
        )


def is_spread_acceptable(
    bid: Decimal,
    ask: Decimal,
    rules: ExecutionRules | None = None,
) -> tuple[bool, float]:
    """Check if bid-ask spread is within acceptable range.

    Returns (acceptable, spread_pct).
    """
    max_spread = (rules or ExecutionRules()).max_spread_pct
    mid = (bid + ask) / 2
    if mid <= 0:
        return (False, 1.0)
    spread_pct = float((ask - bid) / mid)
    return (spread_pct <= max_spread, spread_pct)


def is_in_trading_window(
    current_time: time,
    rules: ExecutionRules | None = None,
) -> tuple[bool, str]:
    """Check if the current time is within acceptable trading hours.

    Returns (ok, reason).
    """
    r = rules or ExecutionRules()

    market_open = time(9, 30)
    market_close = time(16, 0)

    if current_time < market_open or current_time >= market_close:
        return (False, "Market is closed")

    if r.avoid_first_15_min and current_time < time(9, 45):
        return (False, "Avoiding first 15 minutes after open")

    if r.avoid_last_15_min and current_time >= time(15, 45):
        return (False, "Avoiding last 15 minutes before close")

    return (True, "Within trading window")


def estimate_fill_cost(
    contracts: int,
    premium: Decimal,
    rules: ExecutionRules | None = None,
) -> dict[str, Decimal]:
    """Estimate total cost of a fill including slippage and commissions."""
    r = rules or ExecutionRules()
    gross = premium * contracts * 100
    slippage = r.slippage_per_contract * contracts * 100
    commission = r.commission_per_contract * contracts

    return {
        "gross": gross,
        "slippage": slippage,
        "commission": commission,
        "net": gross - slippage - commission,
    }
