"""Account routing — tax-optimal trade placement across accounts.

Routes trades to the best account considering options level,
buying power, liquidity constraints, and tax efficiency.
"""

from __future__ import annotations

from decimal import Decimal

from src.models.account import AccountRouter, BrokerageAccount
from src.models.analysis import SizedOpportunity


def recommend_account(
    router: AccountRouter,
    trade: SizedOpportunity,
) -> tuple[str, str]:
    """Find the best account for a trade.

    Returns (account_id, reasoning).
    Prioritizes: tax efficiency > buying power > options level.
    """
    eligible: list[tuple[str, BrokerageAccount, str]] = []

    for acct_id, acct in router.accounts.items():
        # Check options level
        strategy = trade.trade_type
        if strategy in ("sell_put", "monthly_put", "weekly_put"):
            required_level = 2
        elif strategy in ("put_spread", "call_spread"):
            required_level = 3
        elif strategy in ("strangle", "earnings_crush"):
            required_level = 4
        else:
            required_level = 2

        if acct.options_level < required_level:
            continue

        # Check buying power
        if acct.buying_power < trade.capital_deployed:
            continue

        # Check liquidity constraint
        if _would_violate_liquidity(router, acct_id, trade.capital_deployed):
            continue

        # Tax optimization reasoning
        if acct.account_type == "roth_ira":
            reason = "Roth IRA: tax-free premium income"
            eligible.append((acct_id, acct, reason))
        elif acct.account_type == "taxable":
            reason = "Taxable: full flexibility, margin available"
            eligible.append((acct_id, acct, reason))
        elif acct.account_type == "traditional_ira":
            reason = "Traditional IRA: tax-deferred income"
            eligible.append((acct_id, acct, reason))

    if not eligible:
        return ("", "No eligible account found")

    # Priority: Roth > Traditional > Taxable for premium income
    # But strangles/spreads must go to Taxable (Level 3+)
    needs_high_level = strategy in ("strangle", "put_spread", "earnings_crush")

    for acct_id, acct, reason in eligible:
        if needs_high_level and acct.account_type == "taxable":
            return (acct_id, f"Taxable (requires Level {required_level}): {reason}")

    # For standard puts: Roth first
    for acct_id, acct, reason in eligible:
        if acct.account_type == "roth_ira":
            tax_saved = _estimate_tax_savings(trade, router)
            return (
                acct_id,
                f"{reason} (saves ~${tax_saved:,.0f} in taxes)",
            )

    # Fallback to first eligible
    acct_id, _, reason = eligible[0]
    return (acct_id, reason)


def check_liquidity_health(router: AccountRouter) -> tuple[bool, str]:
    """Check if portfolio meets liquidity constraints.

    Returns (healthy, warning_or_ok).
    """
    total = router.total_nlv
    liquid = router.total_liquid
    ratio = router.liquidity_ratio

    emergency_reserve = router.monthly_expenses * router.emergency_reserve_months
    issues: list[str] = []

    if ratio < router.min_liquid_pct:
        issues.append(
            f"Liquidity ratio {ratio:.1%} below "
            f"minimum {router.min_liquid_pct:.0%}"
        )

    if liquid < router.min_liquid_dollars:
        issues.append(
            f"Liquid assets ${liquid:,.0f} below "
            f"minimum ${router.min_liquid_dollars:,.0f}"
        )

    if liquid < emergency_reserve:
        issues.append(
            f"Liquid assets ${liquid:,.0f} below "
            f"emergency reserve ${emergency_reserve:,.0f}"
        )

    # Check if any restricted account > 40% of NLV
    if total > 0:
        for acct_id, acct in router.accounts.items():
            if acct.withdrawal_restricted:
                pct = float(acct.total_value / total)
                if pct > 0.40:
                    issues.append(
                        f"{acct_id} ({acct.account_type}) is "
                        f"{pct:.0%} of NLV — consider routing to taxable"
                    )

    if issues:
        return (False, "; ".join(issues))
    return (True, f"Healthy: {ratio:.0%} liquid, ${liquid:,.0f}")


def _would_violate_liquidity(
    router: AccountRouter,
    acct_id: str,
    capital_needed: Decimal,
) -> bool:
    """Check if deploying capital in an account would violate liquidity."""
    acct = router.accounts.get(acct_id)
    if not acct:
        return True

    # Taxable is always liquid
    if acct.account_type == "taxable":
        return False

    # For IRAs: check if deploying reduces liquid ratio below minimum
    projected_liquid = router.total_liquid
    # Deploying in IRA doesn't reduce liquid (IRA wasn't liquid anyway)
    # But it does reduce buying power, which could trap capital
    if acct.buying_power - capital_needed < Decimal("0"):
        return True

    return False


def _estimate_tax_savings(
    trade: SizedOpportunity,
    router: AccountRouter,
) -> Decimal:
    """Estimate tax savings from routing to Roth vs Taxable."""
    # Premium income * STCG rate (37% + 3.8% NIIT)
    premium_income = trade.premium * trade.contracts * 100
    stcg_rate = Decimal("0.408")
    return (premium_income * stcg_rate).quantize(Decimal("1"))
