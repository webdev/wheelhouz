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


def review_position(position: Position, context: IntelligenceContext) -> PositionReview:
    """Review a single open position against current intelligence.

    Priority order:
    1. CLOSE NOW — loss stop hit, earnings conflict, intelligence consensus bearish
    2. TAKE PROFIT — captured >75% of premium
    3. WATCH CLOSELY — something changed (TV flipped, trend weakening)
    4. HOLD — thesis intact, everything healthy
    """
    pnl = position.entry_price - position.current_price  # positive = profit for short
    pnl_pct = float(pnl / position.entry_price) if position.entry_price > 0 else 0.0
    loss_multiple = float(position.current_price / position.entry_price) if position.entry_price > 0 else 0.0

    reasons: list[str] = []

    # 1. CLOSE NOW checks
    # Loss stop: 2x premium for monthlies, 1.5x for weeklies (DTE <= 10)
    stop_threshold = 1.5 if position.days_to_expiry <= 10 else 2.0
    if loss_multiple >= stop_threshold:
        reasons.append(f"Loss stop hit: current ${position.current_price} is "
                       f"{loss_multiple:.1f}x entry ${position.entry_price}")
        return PositionReview(
            symbol=position.symbol, action="CLOSE NOW",
            reasoning=". ".join(reasons),
            current_pnl=pnl * 100 * position.quantity,
            days_to_expiry=position.days_to_expiry,
        )

    # Earnings conflict
    if context.portfolio.earnings_conflict:
        reasons.append("Earnings within expiration window")
        return PositionReview(
            symbol=position.symbol, action="CLOSE NOW",
            reasoning=". ".join(reasons),
            current_pnl=pnl * 100 * position.quantity,
            days_to_expiry=position.days_to_expiry,
        )

    # 2. TAKE PROFIT — captured >75% of premium
    if pnl_pct >= 0.75:
        reasons.append(f"Captured {pnl_pct:.0%} of premium "
                       f"(${pnl * 100 * position.quantity:,.0f} profit)")
        return PositionReview(
            symbol=position.symbol, action="TAKE PROFIT",
            reasoning=". ".join(reasons),
            current_pnl=pnl * 100 * position.quantity,
            days_to_expiry=position.days_to_expiry,
        )

    # 3. WATCH CLOSELY checks
    watch_reasons: list[str] = []

    # TradingView flipped strongly bearish
    tc = context.technical_consensus
    if tc and tc.overall in ("SELL", "STRONG_SELL"):
        watch_reasons.append(f"TradingView consensus: {tc.overall}")

    # Trend is downtrend for a short put position
    if (context.quant.trend_direction == "downtrend"
            and position.position_type == "short_put"):
        watch_reasons.append("Price in confirmed downtrend")

    # IV dropped significantly (premium likely cheap now)
    if context.quant.iv_rank < 30 and context.quant.iv_rank > 0:
        watch_reasons.append(f"IV rank dropped to {context.quant.iv_rank:.0f}")

    if watch_reasons:
        return PositionReview(
            symbol=position.symbol, action="WATCH CLOSELY",
            reasoning=". ".join(watch_reasons),
            current_pnl=pnl * 100 * position.quantity,
            days_to_expiry=position.days_to_expiry,
        )

    # 4. HOLD — everything healthy
    hold_reasons = ["Thesis intact"]
    if tc:
        hold_reasons.append(f"TradingView: {tc.overall}")
    hold_reasons.append(f"Trend: {context.quant.trend_direction}")
    if pnl > 0:
        hold_reasons.append(f"P&L: +${pnl * 100 * position.quantity:,.0f}")

    return PositionReview(
        symbol=position.symbol, action="HOLD",
        reasoning=". ".join(hold_reasons),
        current_pnl=pnl * 100 * position.quantity,
        days_to_expiry=position.days_to_expiry,
    )


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
