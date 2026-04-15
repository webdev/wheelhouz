# src/intelligence/position_review.py
"""Position review — evaluate open positions for hold/watch/close.

The same intelligence that decides entries continuously re-evaluates
open positions. If the system wouldn't recommend opening the trade
today, it tells you to consider closing it.

Roll recommendations are Greek-aware: they pick strikes based on delta
targets, stress-test for 10%/20% drops, and flag high-risk situations.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import structlog

from src.models.intelligence import IntelligenceContext
from src.models.market import OptionsChain
from src.models.position import Position

logger = structlog.get_logger()

# Risk parameters for roll strike selection
_PUT_DELTA_TARGET = 0.22       # sweet spot for wheel puts
_PUT_DELTA_MAX = 0.30          # never roll to delta above this
_PUT_DELTA_HIGH_IV_TARGET = 0.16  # go further OTM when IV is elevated
_PUT_DELTA_HIGH_IV_MAX = 0.22
_HIGH_IV_THRESHOLD = 60        # IV rank above this = high IV environment
_MAX_RISK_REWARD = 3.0         # block rolls where 10% drop loss > 3x premium
_LARGE_PROFIT_THRESHOLD = Decimal("2000")  # flag positions with $2K+ captured


def _snap_to_expiration(target: date, chain: OptionsChain | None) -> date:
    """Snap a computed date to the nearest real options expiration.

    Uses chain.expirations if available. Falls back to snapping to the
    nearest Friday (standard options expiration day).
    """
    if chain and chain.expirations:
        # Find the nearest expiration on or after the target date
        future_exps = [e for e in chain.expirations if e >= target]
        if future_exps:
            return min(future_exps)
        # All expirations are before target — pick the latest one
        return max(chain.expirations)

    # Fallback: snap to the nearest Friday
    days_until_friday = (4 - target.weekday()) % 7  # 4 = Friday
    if days_until_friday == 0:
        return target
    return target + __import__("datetime").timedelta(days=days_until_friday)


# Stocks that routinely move 10%+ on earnings — stronger close recommendation
_HIGH_VOL_EARNINGS_MOVERS = frozenset({
    "TSLA", "NVDA", "META", "NFLX", "SNAP", "ROKU", "SHOP", "COIN",
    "PLTR", "AFRM", "UPST", "MARA", "RIOT", "SMCI", "ARM", "MU",
    "SOFI", "HOOD", "RBLX", "PINS", "TTD", "CRWD", "DDOG", "NET",
})


@dataclass
class RollRisk:
    """Risk metrics for a proposed roll."""
    delta: float               # delta of the new position
    gamma: float               # gamma — how fast delta changes
    iv: float                  # implied volatility of the new strike
    loss_at_10pct_drop: Decimal  # dollar loss if stock drops 10% (per contract)
    loss_at_20pct_drop: Decimal  # dollar loss if stock drops 20% (per contract)
    risk_reward: float         # loss_at_10pct_drop / premium (lower = better)
    collateral: Decimal        # capital tied up (strike × 100)
    warnings: list[str] = field(default_factory=list)


@dataclass
class RollRecommendation:
    """A specific roll suggestion: close current, open new."""
    close_price: Decimal          # per-contract cost to buy back
    new_strike: Decimal           # strike for the new position
    new_expiration: str           # formatted date (e.g. "May 14")
    new_premium: Decimal          # per-contract premium for new position
    net_credit: Decimal           # per-contract: new_premium - close_price
    total_net: Decimal            # all contracts: net_credit * quantity * 100
    roll_type: str                # "out", "down_and_out", "up_and_out"
    risk: RollRisk | None = None  # Greek-aware risk assessment


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
    roll: RollRecommendation | None = None


def _make_review(
    position: Position, action: str, reasoning: str,
    pnl: Decimal, pnl_pct: float,
    roll: RollRecommendation | None = None,
) -> PositionReview:
    """Build a PositionReview with full position details."""
    if position.expiration:
        # Show year for expirations beyond the current calendar year
        fmt = "%b %d '%y" if position.expiration.year > date.today().year else "%b %d"
        exp_str = position.expiration.strftime(fmt)
    else:
        exp_str = ""
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
        roll=roll,
    )


def _stress_test_put(
    strike: float, premium: float, current_price: float,
) -> tuple[Decimal, Decimal]:
    """Calculate loss at 10% and 20% stock drops for a short put.

    Returns (loss_at_10pct, loss_at_20pct) per contract.
    Positive = loss, negative = still profitable.
    """
    results = []
    for drop_pct in (0.10, 0.20):
        new_price = current_price * (1 - drop_pct)
        if new_price < strike:
            # ITM: lose intrinsic value minus premium collected
            intrinsic_loss = strike - new_price
            net_loss = intrinsic_loss - premium
        else:
            # Still OTM: keep all premium
            net_loss = -premium
        results.append(Decimal(str(round(net_loss * 100, 0))))  # per contract

    return results[0], results[1]


def _build_roll(
    position: Position,
    context: IntelligenceContext,
    chain: OptionsChain | None = None,
) -> RollRecommendation | None:
    """Build a risk-managed roll recommendation.

    Uses the full options chain (when available) to pick a strike
    based on target delta, IV environment, and stress test outcomes.
    Falls back to IntelligenceContext.options.best_strike when no chain.
    """
    from datetime import timedelta

    close_price = position.current_price
    if close_price <= 0:
        return None

    iv_rank = context.quant.iv_rank
    current_price = float(context.market.price) if context.market else 0.0
    if current_price <= 0:
        return None

    # Roll target must be AFTER the current position's expiration.
    # Start from whichever is later: 30 days from now, or 1 day after
    # current expiration (a roll by definition goes to a later date).
    earnings_date = context.events.next_earnings if context.events else None
    default_target = date.today() + timedelta(days=30)
    if position.expiration and default_target <= position.expiration:
        default_target = position.expiration + timedelta(days=1)

    new_exp_date = default_target
    # Earnings check: if earnings fall within the target window,
    # push the roll target to AFTER the earnings report instead of blocking.
    earnings_in_window = (
        earnings_date is not None
        and earnings_date <= new_exp_date
    )
    if earnings_in_window:
        # Target 30 days AFTER earnings — roll past the report
        new_exp_date = earnings_date + timedelta(days=30)

    # Snap to a real options expiration (nearest Friday or chain expiration)
    new_exp_date = _snap_to_expiration(new_exp_date, chain)

    # Final guard: the snapped date must be after current expiration.
    # (Snap could land on the same date if chain has no later expirations.)
    if position.expiration and new_exp_date <= position.expiration:
        logger.info("roll_blocked", symbol=position.symbol,
                    reason="no_later_expiration_available",
                    current_exp=str(position.expiration),
                    target_exp=str(new_exp_date))
        return None

    if position.position_type == "short_put":
        # Never roll a put to a higher strike — that's closer to ATM,
        # more assignment risk. Only roll down (lower strike) or same strike out.
        contract = _pick_put_roll_target(
            chain, current_price, iv_rank,
            max_strike=float(position.strike),
            target_expiration=new_exp_date,
        )

        if contract:
            new_strike = contract.strike
            new_premium = contract.mid
            delta = contract.delta
            gamma = 0.0
            iv = contract.implied_vol
        else:
            # No viable put roll at or below current strike
            return None

        if new_premium <= 0:
            return None

        # Economics gate: if net debit > new premium, you're paying more
        # to roll than you'll collect — just close instead
        net = new_premium - close_price
        if net < 0 and abs(net) > new_premium:
            logger.info("roll_blocked", symbol=position.symbol,
                        reason="debit_exceeds_premium",
                        debit=str(abs(net)), new_premium=str(new_premium))
            return None

        # Stress test
        loss_10, loss_20 = _stress_test_put(
            float(new_strike), float(new_premium), current_price,
        )
        premium_per_contract = float(new_premium) * 100
        risk_reward = float(loss_10) / premium_per_contract if premium_per_contract > 0 else 99.0

        # Build warnings
        warnings: list[str] = []
        if abs(delta) > _PUT_DELTA_MAX:
            warnings.append(f"HIGH DELTA ({delta:.2f}) — close to the money")
        if iv_rank > _HIGH_IV_THRESHOLD:
            warnings.append(f"HIGH IV (rank {iv_rank:.0f}) — stock pricing big moves")
        if earnings_in_window:
            days_to_earnings = (earnings_date - date.today()).days
            warnings.append(f"EARNINGS in {days_to_earnings}d — rolling to post-earnings exp")
        if risk_reward > _MAX_RISK_REWARD:
            warnings.append(f"RISK/REWARD {risk_reward:.1f}:1 — 10% drop wipes out {risk_reward:.0f}x premium")
        if float(new_strike) * 100 > 50000:
            warnings.append(f"LARGE COLLATERAL ${float(new_strike) * 100:,.0f}")

        # Block roll if risk/reward is unacceptable
        if risk_reward > _MAX_RISK_REWARD:
            logger.info("roll_blocked", symbol=position.symbol,
                        reason="risk_reward", risk_reward=round(risk_reward, 1))
            return None

        # Roll type
        if new_strike < position.strike:
            roll_type = "down_and_out"
        elif new_strike > position.strike:
            roll_type = "up_and_out"
        else:
            roll_type = "out"

        collateral = Decimal(str(float(new_strike) * 100))

    elif position.position_type == "short_call":
        # For calls, pick from the call chain — must be at or above current strike
        contract = _pick_call_roll_target(
            chain, current_price, iv_rank,
            min_strike=float(position.strike),
            target_expiration=new_exp_date,
        )

        if contract:
            new_strike = contract.strike
            new_premium = contract.mid
            delta = contract.delta
            iv = contract.implied_vol
        else:
            # No viable call roll at or above current strike
            return None

        if new_premium <= 0:
            return None

        # Economics gate: if net debit > new premium, you're paying more
        # to roll than you'll collect — just close instead
        net = new_premium - close_price
        if net < 0 and abs(net) > new_premium:
            logger.info("roll_blocked", symbol=position.symbol,
                        reason="debit_exceeds_premium",
                        debit=str(abs(net)), new_premium=str(new_premium))
            return None

        warnings = []
        if abs(delta) > 0.35:
            warnings.append(f"HIGH DELTA ({delta:.2f}) — close to the money")
        if earnings_in_window:
            days_to_earnings = (earnings_date - date.today()).days
            warnings.append(f"EARNINGS in {days_to_earnings}d — rolling to post-earnings exp")

        premium_per_contract = float(new_premium) * 100
        # For calls on owned stock, "loss" is opportunity cost, not real loss
        loss_10 = Decimal("0")
        loss_20 = Decimal("0")
        risk_reward = 0.0
        collateral = Decimal("0")

        if new_strike > position.strike:
            roll_type = "up_and_out"
        else:
            roll_type = "out"
    else:
        return None

    net_credit = new_premium - close_price
    total_net = net_credit * 100 * position.quantity

    risk = RollRisk(
        delta=delta,
        gamma=gamma if position.position_type == "short_put" else 0.0,
        iv=iv,
        loss_at_10pct_drop=loss_10,
        loss_at_20pct_drop=loss_20,
        risk_reward=risk_reward,
        collateral=collateral,
        warnings=warnings,
    )

    return RollRecommendation(
        close_price=close_price,
        new_strike=new_strike,
        new_expiration=new_exp_date.strftime(
            "%b %d '%y" if new_exp_date.year > date.today().year else "%b %d"
        ),
        new_premium=new_premium,
        net_credit=net_credit,
        total_net=total_net,
        roll_type=roll_type,
        risk=risk,
    )


def _pick_put_roll_target(
    chain: OptionsChain | None, current_price: float, iv_rank: float,
    max_strike: float = float("inf"),
    target_expiration: date | None = None,
) -> object | None:
    """Pick the best OTM put for a roll based on delta and IV environment.

    Never rolls up — max_strike ensures the new strike is at or below
    the current position's strike. Rolling a put to a higher strike
    increases assignment risk.

    High IV → go further OTM (lower delta) to reduce assignment risk.
    Normal IV → target the wheel sweet spot (0.20-0.25 delta).

    When target_expiration is given, only considers contracts at that
    expiration so the displayed date matches the actual contract.
    """
    if not chain or not chain.puts:
        return None

    # Delta targets based on IV environment
    if iv_rank > _HIGH_IV_THRESHOLD:
        target_delta = _PUT_DELTA_HIGH_IV_TARGET
        max_delta = _PUT_DELTA_HIGH_IV_MAX
    else:
        target_delta = _PUT_DELTA_TARGET
        max_delta = _PUT_DELTA_MAX

    # Base filter: OTM, has bid, at or below current strike
    base = [
        p for p in chain.puts
        if float(p.strike) < current_price
        and float(p.strike) <= max_strike
        and p.bid > 0
    ]

    # Filter to target expiration when specified
    if target_expiration:
        base = [p for p in base if p.expiration == target_expiration]

    if not base:
        return None

    # Prefer contracts within delta range
    candidates = [
        p for p in base
        if 0.05 <= abs(p.delta) <= max_delta
    ]

    if not candidates:
        candidates = base

    # Pick nearest to target delta
    return min(candidates, key=lambda c: abs(abs(c.delta) - target_delta))


def _pick_call_roll_target(
    chain: OptionsChain | None, current_price: float, iv_rank: float,
    min_strike: float = 0.0,
    target_expiration: date | None = None,
) -> object | None:
    """Pick the best OTM call for a covered call roll.

    Never rolls down — min_strike ensures the new strike is at or above
    the current position's strike. Rolling a call to a lower strike
    increases assignment risk and costs a big debit.

    When target_expiration is given, only considers contracts at that
    expiration so the displayed date matches the actual contract.
    """
    if not chain or not chain.calls:
        return None

    target_delta = 0.25 if iv_rank <= _HIGH_IV_THRESHOLD else 0.18
    max_delta = 0.35

    # Base filter: OTM, has bid, at or above current strike
    base = [
        c for c in chain.calls
        if float(c.strike) > current_price
        and float(c.strike) >= min_strike
        and c.bid > 0
    ]

    # Filter to target expiration when specified
    if target_expiration:
        base = [c for c in base if c.expiration == target_expiration]

    if not base:
        return None

    # Prefer contracts within delta range
    candidates = [
        c for c in base
        if 0.05 <= abs(c.delta) <= max_delta
    ]

    if not candidates:
        candidates = base

    return min(candidates, key=lambda c: abs(abs(c.delta) - target_delta))


def review_position(
    position: Position,
    context: IntelligenceContext,
    chain: OptionsChain | None = None,
) -> PositionReview:
    """Review a single open position against current intelligence.

    Priority order:
    1. CLOSE NOW — loss stop hit, earnings imminent + short-dated
    2. TAKE PROFIT — captured >50% of premium
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

    # Build roll recommendation upfront (used by multiple actions)
    roll = _build_roll(position, context, chain) if is_short else None

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
            return _make_review(position, "CLOSE NOW", reason, pnl, pnl_pct, roll=roll)

    # Earnings conflict — only flag if earnings falls BEFORE the position's
    # expiration. A put expiring May 1 doesn't care about May 5 earnings.
    earnings_date = context.events.next_earnings if context.events else None
    earnings_before_expiry = (
        earnings_date is not None
        and position.expiration is not None
        and earnings_date < position.expiration
    )
    if earnings_before_expiry and position.days_to_expiry <= 30:
        days_to_earnings = (earnings_date - date.today()).days
        if roll:
            reason = f"Earnings in {days_to_earnings}d, position expires after — close or roll before report"
        else:
            reason = f"Earnings in {days_to_earnings}d, position expires after — close before report"
        return _make_review(position, "CLOSE NOW", reason, pnl, pnl_pct, roll=roll)

    # 2. TAKE PROFIT — buy back short options to capture profit and redeploy
    # Scale threshold by moneyness AND time remaining.
    # Deep OTM with short DTE: let it ride (theta accelerating).
    # Deep OTM with long DTE: take profit sooner — remaining premium decays
    # very slowly while collateral stays locked. Better to close and redeploy.
    pos_delta = abs(position.delta)
    dte = position.days_to_expiry
    if pos_delta < 0.10:
        if dte > 120:
            take_profit_threshold = 0.50  # deep OTM + long-dated — close and redeploy
        elif dte > 60:
            take_profit_threshold = 0.65  # deep OTM + medium-dated
        else:
            take_profit_threshold = 0.80  # deep OTM + short-dated — let it ride
    elif pos_delta > 0.25:
        take_profit_threshold = 0.40  # near ATM — take profit early
    else:
        take_profit_threshold = 0.50  # moderate OTM — standard

    if is_short and pnl_pct >= take_profit_threshold:
        profit_dollars = pnl * 100 * position.quantity
        close_cost = position.current_price * 100 * position.quantity
        if pnl_pct >= 0.75:
            reason = (f"Captured {pnl_pct:.0%} of premium (${profit_dollars:,.0f} profit) — "
                      f"buy to close for ${close_cost:,.0f}, almost free money left on table")
        else:
            reason = (f"Captured {pnl_pct:.0%} of premium (${profit_dollars:,.0f} profit) — "
                      f"buy to close for ${close_cost:,.0f} and sell next month's cycle")
        return _make_review(position, "TAKE PROFIT", reason, pnl, pnl_pct, roll=roll)

    # 2b. Time-based close: under 21 DTE, gamma risk rises, diminishing returns
    if is_short and position.days_to_expiry <= 21 and pnl_pct >= 0.30:
        profit_dollars = pnl * 100 * position.quantity
        close_cost = position.current_price * 100 * position.quantity
        reason = (f"Only {position.days_to_expiry}d left with {pnl_pct:.0%} captured "
                  f"(${profit_dollars:,.0f} profit) — gamma risk rising, "
                  f"buy to close for ${close_cost:,.0f} and roll to next month")
        return _make_review(position, "TAKE PROFIT", reason, pnl, pnl_pct, roll=roll)

    # 2c. High-IV + short-dated: captured >50%, DTE ≤ 45, elevated IV.
    # Deep OTM threshold is 80% but with limited time left and fat IV,
    # the remaining premium decays slowly while collateral stays locked.
    # Better to close, free collateral, and redeploy into a fresh cycle.
    iv_rank = context.quant.iv_rank
    if (is_short and pnl_pct >= 0.50
            and position.days_to_expiry <= 45
            and iv_rank > 60):
        profit_dollars = pnl * 100 * position.quantity
        close_cost = position.current_price * 100 * position.quantity
        reason = (f"Captured {pnl_pct:.0%} with {position.days_to_expiry}d left in "
                  f"high IV (rank {iv_rank:.0f}) — close for ${profit_dollars:,.0f} profit "
                  f"and redeploy into a richer cycle")
        return _make_review(position, "TAKE PROFIT", reason, pnl, pnl_pct, roll=roll)

    # 2d. High-vol earnings movers with significant profit: close before report.
    # These names routinely move 10%+ on earnings — locking in ≥50% of premium
    # before the report is the smart play, even on long-dated positions.
    if (is_short and pnl_pct >= 0.50
            and earnings_before_expiry
            and position.symbol in _HIGH_VOL_EARNINGS_MOVERS):
        earnings_date_val = context.events.next_earnings
        days_to_earnings = (earnings_date_val - date.today()).days
        profit_dollars = pnl * 100 * position.quantity
        close_cost = position.current_price * 100 * position.quantity
        reason = (f"{position.symbol} reports in {days_to_earnings}d and routinely moves 10%+ — "
                  f"close to lock in ${profit_dollars:,.0f} profit ({pnl_pct:.0%} captured), "
                  f"buy to close for ${close_cost:,.0f} and re-sell after IV crush")
        return _make_review(position, "TAKE PROFIT", reason, pnl, pnl_pct, roll=roll)

    # 3. WATCH CLOSELY checks — every reason must say WHAT TO DO
    watch_reasons: list[str] = []

    # 3a. Large absolute profit that didn't hit % threshold
    if is_short and pnl > 0:
        profit_dollars = pnl * 100 * position.quantity
        if profit_dollars >= _LARGE_PROFIT_THRESHOLD and pnl_pct < take_profit_threshold:
            watch_reasons.append(
                f"${profit_dollars:,.0f} profit captured ({pnl_pct:.0%}) with "
                f"{position.days_to_expiry}d left — consider closing to lock in gains, "
                f"especially before any catalyst")

    # Earnings within window but long-dated
    if earnings_before_expiry and position.days_to_expiry > 30:
        days_to_earnings = (earnings_date - date.today()).days
        # Prescriptive advice depends on moneyness and P&L
        if pos_delta < 0.10 and pnl_pct >= 0.40:
            is_big_mover = position.symbol in _HIGH_VOL_EARNINGS_MOVERS
            profit_dollars = pnl * 100 * position.quantity
            if is_big_mover and pnl_pct >= 0.50:
                # High-vol names with significant profit: recommend closing
                watch_reasons.append(
                    f"Earnings in {days_to_earnings}d. {position.symbol} routinely moves "
                    f"10%+ on reports — close to lock in ${profit_dollars:,.0f} profit "
                    f"({pnl_pct:.0%} captured) and re-sell after IV crush")
            else:
                watch_reasons.append(
                    f"Earnings in {days_to_earnings}d. Deep OTM with {pnl_pct:.0%} captured — "
                    f"safe to hold through report unless gap risk concerns you. "
                    f"Close before if you want to lock in profit")
        elif pnl_pct >= 0.30:
            watch_reasons.append(
                f"Earnings in {days_to_earnings}d with {pnl_pct:.0%} captured. "
                f"Consider closing before report to lock profit, then re-sell after IV crush")
        elif pnl_pct < 0:
            if pos_delta < 0.10 and pnl_pct > -0.10:
                # Deep OTM with small loss — earnings unlikely to threaten
                watch_reasons.append(
                    f"Earnings in {days_to_earnings}d. Position is deep OTM "
                    f"(delta {pos_delta:.2f}) — unlikely to be threatened by report. "
                    f"Hold unless earnings surprise dramatically")
            else:
                if roll:
                    watch_reasons.append(
                        f"Earnings in {days_to_earnings}d and position is underwater ({pnl_pct:.0%}). "
                        f"Close before report to limit loss, or roll to post-earnings expiration")
                else:
                    watch_reasons.append(
                        f"Earnings in {days_to_earnings}d and position is underwater ({pnl_pct:.0%}). "
                        f"Close before report to limit loss")
        else:
            # Calculate the 5% trigger level
            trigger_pct = 0.05
            if position.position_type == "short_put":
                trigger_price = float(position.strike) * (1 + trigger_pct)
                direction = "drops to"
                action = "buy to close the put"
                risk = (f"At-the-money puts through earnings can lose "
                        f"${float(position.strike) * 0.10 * 100:,.0f}+ on a 10% gap down")
            else:
                trigger_price = float(position.strike) * (1 - trigger_pct)
                direction = "rallies to"
                action = "buy to close the call"
                risk = (f"At-the-money calls through earnings risk assignment "
                        f"if stock gaps above ${position.strike}")
            watch_reasons.append(
                f"Earnings in {days_to_earnings}d. "
                f"Set alert at ${trigger_price:,.0f} — "
                f"if stock {direction} that level, {action} immediately. "
                f"{risk}")

    # TradingView flipped strongly bearish
    tc = context.technical_consensus
    if tc and tc.overall in ("SELL", "STRONG_SELL"):
        if position.position_type == "short_put":
            watch_reasons.append(
                f"TV {tc.overall} — crowd is bearish. "
                f"Tighten your stop or close if price breaks below ${position.strike}")
        elif position.position_type == "short_call":
            watch_reasons.append(
                f"TV {tc.overall} — bearish momentum favors your short call. "
                f"Hold unless stock reverses sharply")
        else:
            watch_reasons.append(f"TV {tc.overall}")

    # Trend is downtrend for a short put position
    if (context.quant.trend_direction == "downtrend"
            and position.position_type == "short_put"):
        if position.days_to_expiry <= 21:
            watch_reasons.append(
                f"Downtrend with only {position.days_to_expiry}d left — "
                f"close or roll out if stock breaks ${position.strike}")
        else:
            watch_reasons.append(
                f"Price in confirmed downtrend. "
                f"Watch ${position.strike} strike — roll down if threatened")

    # IV dropped significantly (premium likely cheap now)
    if context.quant.iv_rank < 30 and context.quant.iv_rank > 0:
        watch_reasons.append(
            f"IV rank {context.quant.iv_rank:.0f} — premium dried up. "
            f"Not worth opening new positions here, but existing ones can ride")

    if watch_reasons:
        reasoning = watch_reasons[0] if len(watch_reasons) == 1 else "\n".join(f"• {r}" for r in watch_reasons)
        return _make_review(position, "WATCH CLOSELY", reasoning, pnl, pnl_pct, roll=roll)

    # 4. HOLD — everything healthy
    hold_reasons = ["Thesis intact"]
    if tc:
        hold_reasons.append(f"TV {tc.overall}")
    hold_reasons.append(f"Trend: {context.quant.trend_direction}")

    return _make_review(position, "HOLD", ". ".join(hold_reasons), pnl, pnl_pct, roll=roll)


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
