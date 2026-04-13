"""Tax alert generation — wash sales, LTCG tracking, harvesting, quarterly estimates.

Feeds into the morning briefing TAX ALERTS section.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from src.models.tax import TaxContext, TaxEngine, WashSaleTracker


def generate_tax_alerts(
    positions: list[TaxContext],
    wash_tracker: WashSaleTracker | None = None,
) -> list[str]:
    """Generate tax alerts for positions approaching key thresholds.

    Returns a list of alert strings for the briefing.
    """
    alerts: list[str] = []
    tracker = wash_tracker or WashSaleTracker()

    # Wash sale blocks
    for ticker in tracker.get_blocked_tickers():
        window_end = tracker.active_windows.get(ticker)
        if window_end:
            days_left = (window_end - date.today()).days
            alerts.append(
                f"{ticker}: wash sale window (closes {window_end}). "
                f"No new {ticker} trades for {days_left} days."
            )

    for ctx in positions:
        # LTCG approaching — don't sell
        if not ctx.is_ltcg and ctx.days_until_ltcg is not None:
            if 0 < ctx.days_until_ltcg <= 60 and ctx.unrealized_gain > 5000:
                alerts.append(
                    f"{ctx.symbol}: LTCG in {ctx.days_until_ltcg} days. "
                    f"Do NOT sell or risk assignment "
                    f"(saves ${ctx.tax_savings_by_waiting:,.0f})."
                )

        # Large STCG exposure
        if not ctx.is_ltcg and ctx.unrealized_gain > 5000:
            tax = ctx.estimated_tax_if_sold
            alerts.append(
                f"{ctx.symbol}: ${ctx.unrealized_gain:,.0f} unrealized STCG "
                f"(tax if sold: ${tax:,.0f})."
            )

        # Tax-loss harvesting opportunity
        if ctx.unrealized_gain < -2000:
            # Check wash sale window first
            ok, _ = tracker.check_before_trade(ctx.symbol)
            if ok:
                alerts.append(
                    f"{ctx.symbol}: ${abs(ctx.unrealized_gain):,.0f} "
                    f"unrealized loss — consider tax-loss harvest."
                )

    return alerts


def generate_tax_section(
    tax_engine: TaxEngine,
    positions: list[TaxContext],
    wash_tracker: WashSaleTracker | None = None,
) -> str:
    """Generate the full TAX ALERTS section for the morning briefing."""
    alerts = generate_tax_alerts(positions, wash_tracker)

    ytd_lines = [
        f"YTD STCG: ${tax_engine.realized_stcg_ytd:,.0f}",
        f"YTD LTCG: ${tax_engine.realized_ltcg_ytd:,.0f}",
        f"YTD losses: ${tax_engine.realized_losses_ytd:,.0f}",
        f"Option premium: ${tax_engine.option_premium_income_ytd:,.0f}",
    ]

    # Estimated tax owed
    net_stcg = tax_engine.realized_stcg_ytd - tax_engine.harvested_losses_ytd
    estimated_tax = (
        net_stcg * Decimal(str(tax_engine.stcg_effective))
        + tax_engine.realized_ltcg_ytd * Decimal(str(tax_engine.ltcg_effective))
    )
    ytd_lines.append(f"Estimated tax owed: ${estimated_tax:,.0f}")

    # Quarterly payment
    quarterly = estimated_tax / 4
    ytd_lines.append(f"Next quarterly estimate: ${quarterly:,.0f}")

    sections = "\n".join(ytd_lines)
    if alerts:
        sections += "\n\n" + "\n".join(alerts)
    else:
        sections += "\n\nNo tax alerts."

    return sections
