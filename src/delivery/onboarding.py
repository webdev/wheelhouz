"""Day 1 onboarding: portfolio intake, gap analysis, transition plan.

Runs ONCE at setup. Discovers accounts, classifies positions into the
three-engine model, identifies gaps, and generates a phased transition plan.
The Telegram interactive flow (run_onboarding) handles user Q&A.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from src.models.enums import PositionType, Urgency
from src.models.onboarding import (
    GapAnalysis,
    PortfolioIntake,
    StockClassification,
    TransitionAction,
    TransitionPlan,
)
from src.models.position import PortfolioState, Position
from src.models.tax import TaxContext


# ── Step 1: Portfolio Intake & Auto-Classification ──────────────


def auto_classify_portfolio(portfolio: PortfolioState) -> PortfolioIntake:
    """Separate positions into options (auto-Engine 2), cash (Engine 3),
    and stocks (need human input). Apply heuristic suggestions for stocks.
    """
    intake = PortfolioIntake(cash=portfolio.cash_available)

    for pos in portfolio.positions:
        if pos.position_type == PositionType.SHORT_PUT.value:
            intake.short_puts.append(pos)
        elif pos.position_type == PositionType.SHORT_CALL.value:
            intake.short_calls.append(pos)
        elif pos.position_type == PositionType.LONG_STOCK.value:
            sc = _classify_stock(pos)
            intake.stock_positions.append(sc)

            # Track tax context
            ctx = _build_tax_context(pos)
            if ctx.unrealized_gain > 0:
                intake.positions_with_gains.append(ctx)
            elif ctx.unrealized_gain < 0:
                intake.positions_with_losses.append(ctx)

            # RSU/ESPP concentration
            if pos.symbol == "ADBE":
                intake.rsu_espp_positions.append(pos)

    return intake


def _classify_stock(pos: Position) -> StockClassification:
    """Apply heuristic engine suggestion for a stock position."""
    sc = StockClassification(
        symbol=pos.symbol,
        shares=abs(pos.quantity),
        cost_basis=pos.cost_basis,
        current_price=pos.underlying_price,
        unrealized_pnl=pos.unrealized_pnl,
        holding_period_days=pos.holding_period_days,
        is_ltcg=pos.holding_period_days >= 365,
    )

    # Heuristic suggestions
    if pos.symbol == "ADBE":
        sc.suggested_engine = "engine2"
        sc.suggestion_reason = "RSU/ESPP concentration — sell down over time"
    elif pos.holding_period_days >= 365 and pos.unrealized_pnl > 0:
        sc.suggested_engine = "engine1"
        sc.suggestion_reason = "LTCG with gains — likely a compounder"
    elif abs(pos.quantity) == 100 and pos.holding_period_days < 60:
        sc.suggested_engine = "engine2"
        sc.suggestion_reason = "Round lot, recently acquired — likely from put assignment"
    else:
        sc.suggested_engine = "engine1"
        sc.suggestion_reason = "Default — classify as core holding"

    return sc


def _build_tax_context(pos: Position) -> TaxContext:
    """Build tax context for a stock position."""
    is_ltcg = pos.holding_period_days >= 365
    gain = pos.unrealized_pnl

    tax_if_sold = _estimate_tax(gain, is_ltcg)
    if is_ltcg:
        tax_if_waited = tax_if_sold  # already LTCG
        savings = Decimal("0")
        days_until = None
    else:
        tax_if_waited = _estimate_tax(gain, is_ltcg=True)
        savings = tax_if_sold - tax_if_waited
        days_until = max(0, 365 - pos.holding_period_days)

    return TaxContext(
        symbol=pos.symbol,
        cost_basis_per_share=pos.cost_basis,
        current_price=pos.underlying_price,
        unrealized_gain=gain,
        unrealized_gain_pct=(
            float(gain / pos.cost_basis) if pos.cost_basis > 0 else 0.0
        ),
        purchase_date=pos.purchase_date,
        holding_period_days=pos.holding_period_days,
        is_ltcg=is_ltcg,
        estimated_tax_if_sold=tax_if_sold,
        estimated_tax_if_waited_for_ltcg=tax_if_waited,
        tax_savings_by_waiting=savings,
        days_until_ltcg=days_until,
    )


def _estimate_tax(gain: Decimal, is_ltcg: bool) -> Decimal:
    """Estimate tax on a gain. STCG 37% + 3.8% NIIT, LTCG 20% + 3.8% NIIT."""
    if gain <= 0:
        return Decimal("0")
    rate = Decimal("0.238") if is_ltcg else Decimal("0.408")
    return (gain * rate).quantize(Decimal("0.01"))


# ── Step 2: Gap Analysis ───────────────────────────────────────


def analyze_gaps(
    intake: PortfolioIntake,
    portfolio: PortfolioState,
) -> GapAnalysis:
    """Compare current portfolio to the target three-engine allocation.

    Identifies critical, important, and optimization issues.
    """
    nlv = portfolio.net_liquidation
    if nlv == 0:
        nlv = Decimal("1000000")

    # Calculate engine allocations from classified positions
    e1_value = Decimal("0")
    e2_value = Decimal("0")

    for sc in intake.stock_positions:
        engine = sc.engine or sc.suggested_engine or "engine1"
        value = sc.current_price * sc.shares
        if engine == "engine1":
            e1_value += value
        else:
            e2_value += value

    # Options are Engine 2
    for pos in intake.short_puts + intake.short_calls:
        e2_value += abs(pos.market_value)

    e3_value = intake.cash
    total = e1_value + e2_value + e3_value
    if total == 0:
        total = nlv

    gap = GapAnalysis(
        engine1_current_pct=float(e1_value / total),
        engine2_current_pct=float(e2_value / total),
        engine3_current_pct=float(e3_value / total),
    )
    gap.engine1_gap = gap.engine1_target_pct - gap.engine1_current_pct
    gap.engine2_gap = gap.engine2_target_pct - gap.engine2_current_pct
    gap.engine3_gap = gap.engine3_target_pct - gap.engine3_current_pct

    gap.current_theta = portfolio.portfolio_theta
    gap.current_delta = portfolio.portfolio_delta

    # ── Critical issues (fix this week) ────────────────────────
    for pos in portfolio.positions:
        if pos.position_type in (PositionType.SHORT_PUT.value, PositionType.SHORT_CALL.value):
            if pos.profit_pct >= 0.80:
                gap.critical_issues.append(
                    f"{pos.symbol} at {pos.profit_pct:.0%} profit — close NOW"
                )
                gap.stranded_profits.append({
                    "symbol": pos.symbol, "profit_pct": pos.profit_pct,
                })
            elif pos.profit_pct >= 0.50 and pos.days_to_expiry > 21:
                gap.critical_issues.append(
                    f"{pos.symbol} at {pos.profit_pct:.0%} profit, "
                    f"{pos.days_to_expiry} DTE — close early and redeploy"
                )
                gap.stranded_profits.append({
                    "symbol": pos.symbol, "profit_pct": pos.profit_pct,
                })

    # ── Important issues (fix this month) ──────────────────────
    # Concentration violations
    for sym, pct in portfolio.concentration.items():
        if pct > 0.10:
            gap.important_issues.append(
                f"{sym} at {pct:.1%} of NLV — over 10% limit, sell down"
            )
            gap.concentration_violations.append({"symbol": sym, "pct": pct})

    # Uncovered stock (Engine 2 stock without covered calls)
    e2_stock_syms = {
        sc.symbol for sc in intake.stock_positions
        if (sc.engine or sc.suggested_engine) == "engine2" and sc.shares >= 100
    }
    covered_syms = {pos.symbol for pos in intake.short_calls}
    for sym in e2_stock_syms - covered_syms:
        gap.important_issues.append(
            f"{sym}: Engine 2 stock with no covered call — sell calls immediately"
        )
        gap.uncovered_stock.append({"symbol": sym})

    # ── Optimization issues (1-3 months) ───────────────────────
    for ctx in intake.positions_with_gains:
        if not ctx.is_ltcg and ctx.days_until_ltcg is not None:
            if ctx.days_until_ltcg <= 90 and ctx.unrealized_gain > 5000:
                gap.optimization_issues.append(
                    f"{ctx.symbol}: LTCG in {ctx.days_until_ltcg} days "
                    f"(saves ${ctx.tax_savings_by_waiting:,.0f}) — do NOT sell"
                )
                gap.tax_traps.append({
                    "symbol": ctx.symbol,
                    "days_until_ltcg": ctx.days_until_ltcg,
                    "savings": ctx.tax_savings_by_waiting,
                })

    for ctx in intake.positions_with_losses:
        if ctx.unrealized_gain < -2000:
            gap.optimization_issues.append(
                f"{ctx.symbol}: ${abs(ctx.unrealized_gain):,.0f} unrealized loss "
                f"— consider tax-loss harvest"
            )

    return gap


# ── Step 3: Transition Plan ────────────────────────────────────


def generate_transition_plan(
    intake: PortfolioIntake,
    gap: GapAnalysis,
    portfolio: PortfolioState,
) -> TransitionPlan:
    """Generate a phased transition plan from gap analysis.

    Principles:
    1. Don't sell winners near LTCG threshold
    2. Don't force-close positions at a loss unless loss stop breached
    3. Let existing short options expire naturally when profitable
    4. Route ALL new trades through the signal system from day 1
    5. Rebalance through new cash flows, not liquidation
    """
    plan = TransitionPlan()

    # ── Immediate: close stranded profits + loss-stop violations
    for item in gap.stranded_profits:
        plan.immediate_actions.append(TransitionAction(
            urgency=Urgency.IMMEDIATE.value,
            action="close",
            symbol=str(item["symbol"]),
            description=f"Close at {item['profit_pct']:.0%} profit — redeploy capital",
            tax_impact=Decimal("0"),  # options income = STCG regardless
            opportunity_cost=Decimal("0"),
            rationale="Stranded profit is dead capital. Close and recycle.",
        ))

    # ── Immediate: sell covered calls on uncovered Engine 2 stock
    for item in gap.uncovered_stock:
        plan.immediate_actions.append(TransitionAction(
            urgency=Urgency.IMMEDIATE.value,
            action="sell_calls",
            symbol=str(item["symbol"]),
            description="Sell covered calls on 100-share lots",
            tax_impact=Decimal("0"),
            opportunity_cost=Decimal("0"),
            rationale="Engine 2 stock should always have calls sold against it.",
        ))

    # ── Short-term: let near-expiry options run
    for pos in intake.short_puts + intake.short_calls:
        if pos.days_to_expiry <= 21 and pos.profit_pct < 0.50:
            plan.short_term_actions.append(TransitionAction(
                urgency=Urgency.SHORT_TERM.value,
                action="hold",
                symbol=pos.symbol,
                description=(
                    f"Let expire naturally — {pos.days_to_expiry} DTE, "
                    f"{pos.profit_pct:.0%} profit"
                ),
                tax_impact=Decimal("0"),
                opportunity_cost=Decimal("0"),
                rationale="Time decay accelerates inside 21 DTE. Let it work.",
            ))

    # ── Medium-term: ADBE concentration sell-down
    adbe_violations = [
        v for v in gap.concentration_violations if v.get("symbol") == "ADBE"
    ]
    if adbe_violations:
        plan.medium_term_actions.append(TransitionAction(
            urgency=Urgency.MEDIUM_TERM.value,
            action="close",
            symbol="ADBE",
            description="Quarterly sell plan to reduce below 15% NLV",
            tax_impact=Decimal("0"),
            opportunity_cost=Decimal("0"),
            rationale="Concentration risk. Sell 25% of excess each quarter.",
        ))

    # ── Medium-term: respect LTCG approaching
    for trap in gap.tax_traps:
        plan.medium_term_actions.append(TransitionAction(
            urgency=Urgency.MEDIUM_TERM.value,
            action="hold",
            symbol=str(trap["symbol"]),
            description=(
                f"Wait {trap['days_until_ltcg']} days for LTCG "
                f"(saves ${trap['savings']:,.0f})"
            ),
            tax_impact=Decimal(str(trap["savings"])),
            opportunity_cost=Decimal("0"),
            rationale="Tax efficiency: never sell near LTCG threshold.",
        ))

    # Estimate total transition time
    total_actions = (
        len(plan.immediate_actions)
        + len(plan.short_term_actions)
        + len(plan.medium_term_actions)
    )
    plan.estimated_transition_weeks = max(4, min(12, total_actions * 2))

    return plan


# ── Onboarding summary ─────────────────────────────────────────


def format_onboarding_summary(
    intake: PortfolioIntake,
    gap: GapAnalysis,
    plan: TransitionPlan,
) -> str:
    """Format the onboarding completion message for Telegram."""
    e1 = f"{gap.engine1_current_pct:.0%}"
    e2 = f"{gap.engine2_current_pct:.0%}"
    e3 = f"{gap.engine3_current_pct:.0%}"

    return (
        "ONBOARDING COMPLETE\n\n"
        f"CURRENT ALLOCATION:\n"
        f"  Engine 1 (Core):   {e1}  (target 45%)\n"
        f"  Engine 2 (Wheel):  {e2}  (target 45%)\n"
        f"  Engine 3 (Powder): {e3}  (target 10%)\n\n"
        f"CRITICAL ACTIONS (this week): {len(gap.critical_issues)}\n"
        f"SHORT-TERM (2-4 weeks): {len(gap.important_issues)}\n"
        f"MEDIUM-TERM (1-3 months): {len(gap.optimization_issues)}\n\n"
        f"Estimated transition: ~{plan.estimated_transition_weeks} weeks\n\n"
        f"The system will start generating daily briefings tomorrow."
    )
