"""Position scanner — classify existing positions into recommended actions.

Scans every open position and returns what to do with it:
close early, roll, let expire, take assignment, or just monitor.
Incorporates alpha signals for close-and-reload decisions.
"""

from __future__ import annotations

from datetime import date

from src.models.enums import PositionAction
from src.models.market import EventCalendar, MarketContext
from src.models.position import Position
from src.models.signals import AlphaSignal


def scan_position(
    pos: Position,
    mkt: MarketContext,
    cal: EventCalendar,
    all_signals: list[AlphaSignal],
) -> tuple[PositionAction, str]:
    """Classify an existing position into a recommended action.

    Key insight: if a position hit 50% profit AND there's a fresh dip
    signal on another name, close early and redeploy capital into the dip.
    """
    # Loss stop check — always first
    if pos.entry_price and pos.entry_price > 0:
        loss_multiple = float(pos.current_price / pos.entry_price)
        weekly = pos.days_to_expiry <= 10
        stop = 1.5 if weekly else 2.0
        if loss_multiple >= stop:
            return (
                PositionAction.CLOSE_EARLY,
                f"Loss stop breached at {loss_multiple:.1f}x entry premium. "
                f"Close immediately.",
            )

    # Close-and-reload: winner + better opportunity elsewhere
    pending_dip_signals = [
        s for s in all_signals
        if s.strength > 60 and s.symbol != pos.symbol
    ]
    if pos.profit_pct >= 0.40 and pending_dip_signals:
        best = max(pending_dip_signals, key=lambda s: s.strength)
        return (
            PositionAction.CLOSE_AND_RELOAD,
            f"Close at {pos.profit_pct:.0%} profit, redeploy into "
            f"{best.symbol} ({best.signal_type.value}, strength {best.strength:.0f}).",
        )

    # Profit target hit
    if pos.profit_pct >= 0.50 and pos.days_to_expiry > 21:
        return (
            PositionAction.CLOSE_EARLY,
            f"{pos.profit_pct:.0%} of max profit captured, "
            f"{pos.days_to_expiry} DTE remaining. Close and redeploy.",
        )

    # Near-expiry and far OTM — let theta finish
    if pos.days_to_expiry <= 5 and abs(pos.delta) < 0.10:
        return (
            PositionAction.LET_EXPIRE,
            "Far OTM, <5 DTE. Let theta finish the job.",
        )

    # Double down on dips (aggressive)
    own_dip_signals = [
        s for s in all_signals
        if s.symbol == pos.symbol and s.strength > 50
    ]
    if (
        pos.position_type == "short_put"
        and 0 < pos.distance_from_strike_pct < 5.0
        and own_dip_signals
        and mkt.iv_rank > 50
    ):
        return (
            PositionAction.DOUBLE_DOWN,
            f"{pos.symbol} testing your strike but dip signals confirm oversold. "
            f"Sell additional puts at lower strike.",
        )

    # Approaching strike — roll or take assignment
    if pos.position_type == "short_put" and pos.distance_from_strike_pct < 3.0:
        return (
            PositionAction.TAKE_ASSIGNMENT,
            "Near strike. Take assignment and sell covered calls.",
        )

    if pos.position_type == "short_call" and pos.distance_from_strike_pct < 2.0:
        return (
            PositionAction.ROLL_OUT_AND_UP,
            "Stock approaching call strike. Roll out and up for credit.",
        )

    # Earnings conflict
    if cal.next_earnings and pos.expiration and cal.next_earnings <= pos.expiration:
        days_to_er = (cal.next_earnings - date.today()).days
        return (
            PositionAction.ALERT_EARNINGS,
            f"Earnings in {days_to_er} days, before {pos.expiration} expiry.",
        )

    # Dividend conflict (calls only)
    if pos.position_type == "short_call":
        if (
            cal.next_ex_dividend
            and pos.expiration
            and cal.next_ex_dividend <= pos.expiration
        ):
            return (
                PositionAction.ALERT_DIVIDEND,
                "Ex-div before expiry. Early assignment risk on calls.",
            )

    return (PositionAction.MONITOR, "Position healthy, no action needed.")
