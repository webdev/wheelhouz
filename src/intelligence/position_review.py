"""Position review — evaluate open positions for hold/watch/close.

The same intelligence that decides entries continuously re-evaluates
open positions. If the system wouldn't recommend opening the trade
today, it tells you to consider closing it.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import structlog

from src.models.intelligence import IntelligenceContext
from src.models.position import Position

logger = structlog.get_logger()


@dataclass
class PositionReview:
    """Review result for a single position."""
    symbol: str
    action: str  # "CLOSE NOW" / "TAKE PROFIT" / "WATCH CLOSELY" / "HOLD"
    reasoning: str
    current_pnl: Decimal
    days_to_expiry: int
    # Position details for display
    option_type: str = ""       # "put" or "call"
    strike: Decimal = Decimal("0")
    expiration: str = ""        # formatted date string
    position_type: str = ""     # "short_put", "short_call", etc.
    quantity: int = 0
    entry_price: Decimal = Decimal("0")
    current_price: Decimal = Decimal("0")
    pnl_pct: float = 0.0       # % of premium captured (positive = profit)


def _make_review(position: Position, action: str, reasoning: str, pnl: Decimal, pnl_pct: float) -> PositionReview:
    """Build a PositionReview with full position details."""
    exp_str = position.expiration.strftime("%b %d") if position.expiration else ""
    # Options are 100 shares per contract
    multiplier = 100 if position.option_type else 1
    return PositionReview(
        symbol=position.symbol,
        action=action,
        reasoning=reasoning,
        current_pnl=pnl * multiplier * position.quantity,
        days_to_expiry=position.days_to_expiry,
        option_type=position.option_type,
        strike=position.strike,
        expiration=exp_str,
        position_type=position.position_type,
        quantity=position.quantity,
        entry_price=position.entry_price,
        current_price=position.current_price,
        pnl_pct=pnl_pct,
    )


def review_position(position: Position, context: IntelligenceContext) -> PositionReview:
    """Review a single open position against current intelligence.

    Priority order:
    1. CLOSE NOW — loss stop hit, earnings imminent + short-dated
    2. TAKE PROFIT — captured >75% of premium
    3. WATCH CLOSELY — something changed (TV flipped, trend weakening, earnings upcoming)
    4. HOLD — thesis intact, everything healthy
    """
    is_short = position.position_type.startswith("short_")

    # P&L: for short options, profit = entry - current (price dropping is good)
    # For long options, profit = current - entry
    if is_short:
        pnl = position.entry_price - position.current_price
    else:
        pnl = position.current_price - position.entry_price
    pnl_pct = float(pnl / position.entry_price) if position.entry_price > 0 else 0.0

    # 1. CLOSE NOW checks
    # Loss stop (short options only): close if option price rises to 2x (monthlies)
    # or 1.5x (weeklies) what you sold it for
    if is_short and position.entry_price > 0:
        loss_multiple = float(position.current_price / position.entry_price)
        stop_threshold = 1.5 if position.days_to_expiry <= 10 else 2.0
        if loss_multiple >= stop_threshold:
            loss_dollars = (position.current_price - position.entry_price) * 100 * position.quantity
            reason = (f"Loss stop hit: now ${position.current_price} vs "
                      f"${position.entry_price} entry ({loss_multiple:.1f}x) — "
                      f"buy back for ${loss_dollars:,.0f} loss")
            return _make_review(position, "CLOSE NOW", reason, pnl, pnl_pct)

    # Earnings conflict — only CLOSE NOW for short-dated positions (DTE <= 30)
    # where earnings is truly imminent. Long-dated options were sold knowing
    # earnings would occur; just flag as WATCH.
    if context.portfolio.earnings_conflict and position.days_to_expiry <= 30:
        reason = "Earnings imminent — close before report"
        return _make_review(position, "CLOSE NOW", reason, pnl, pnl_pct)

    # 2. TAKE PROFIT — captured >75% of premium (short options only)
    if is_short and pnl_pct >= 0.75:
        profit_dollars = pnl * 100 * position.quantity
        reason = f"Captured {pnl_pct:.0%} of premium (${profit_dollars:,.0f} profit) — buy back for ${position.current_price}"
        return _make_review(position, "TAKE PROFIT", reason, pnl, pnl_pct)

    # 3. WATCH CLOSELY checks
    watch_reasons: list[str] = []

    # Earnings within window but long-dated — note it, don't panic
    if context.portfolio.earnings_conflict and position.days_to_expiry > 30:
        watch_reasons.append("Earnings before expiration — monitor around report")

    # TradingView flipped strongly bearish
    tc = context.technical_consensus
    if tc and tc.overall in ("SELL", "STRONG_SELL"):
        watch_reasons.append(f"TV {tc.overall}")

    # Trend is downtrend for a short put position
    if (context.quant.trend_direction == "downtrend"
            and position.position_type == "short_put"):
        watch_reasons.append("Price in confirmed downtrend")

    # IV dropped significantly (premium likely cheap now)
    if context.quant.iv_rank < 30 and context.quant.iv_rank > 0:
        watch_reasons.append(f"IV rank dropped to {context.quant.iv_rank:.0f}")

    if watch_reasons:
        return _make_review(position, "WATCH CLOSELY", ". ".join(watch_reasons), pnl, pnl_pct)

    # 4. HOLD — everything healthy
    hold_reasons = ["Thesis intact"]
    if tc:
        hold_reasons.append(f"TV {tc.overall}")
    hold_reasons.append(f"Trend: {context.quant.trend_direction}")

    return _make_review(position, "HOLD", ". ".join(hold_reasons), pnl, pnl_pct)


def format_position_review(reviews: list[PositionReview]) -> str:
    """Format position reviews for the briefing output."""
    if not reviews:
        return "  No open positions."

    lines: list[str] = []
    action_icons = {
        "CLOSE NOW": "!",
        "TAKE PROFIT": "$",
        "WATCH CLOSELY": "?",
        "HOLD": " ",
    }

    for r in reviews:
        icon = action_icons.get(r.action, " ")
        lines.append(f"  {icon} {r.symbol} — {r.action}")
        lines.append(f"    P&L: ${r.current_pnl:,.0f} | {r.days_to_expiry}d to expiry")
        lines.append(f"    {r.reasoning}")
    return "\n".join(lines)
