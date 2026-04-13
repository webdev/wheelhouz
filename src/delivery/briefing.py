"""Claude-powered briefing generator.

Synthesizes portfolio state, signals, opportunities, and risk into a
morning briefing via Claude API. The system prompt encodes the aggressive
wheel-strategy trading philosophy.
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal

from src.models.analysis import RiskReport, SizedOpportunity
from src.models.enums import PositionAction
from src.models.market import MarketContext
from src.models.position import PortfolioState, Position
from src.models.signals import AlphaSignal


async def generate_briefing(
    portfolio: PortfolioState,
    actions: list[tuple[Position, PositionAction, str]],
    opportunities: list[SizedOpportunity],
    risk: RiskReport,
    signals: list[AlphaSignal],
    macro: MarketContext,
) -> str:
    """Generate a full morning briefing via Claude API.

    Returns the briefing text ready for Telegram delivery.
    """
    import anthropic  # type: ignore[import-not-found]

    client = anthropic.AsyncAnthropic(
        api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
    )

    response = await client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=3000,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": _build_user_prompt(
                portfolio, actions, opportunities, risk, signals, macro,
            ),
        }],
    )

    return response.content[0].text  # type: ignore[union-attr,no-any-return]


SYSTEM_PROMPT = """\
You are an aggressive, professional options trader managing a wheel strategy \
portfolio. You think like a market maker who happens to have directional \
conviction. Your job is to produce a concise morning briefing with concrete, \
sized trade recommendations.

Your trading philosophy:
- SELL INTO FEAR. Red days are paydays. When VIX spikes and stocks dip, \
premiums get fat — load up.
- EVERY DOLLAR should be working. Idle cash is a losing position. Flag idle \
capital as a problem.
- SIZE BY CONVICTION. High-signal dip setup gets 3-5% of NLV. Marginal IV \
play gets 1%. Never flat-size.
- PICK STRIKES AT LEVELS, not deltas. 200 SMA with -0.30 delta beats -0.25 \
delta in no-man's land.
- CLOSE WINNERS AND REDEPLOY. Position at 50% profit with 30 DTE remaining \
is dead capital. Recycle it.
- ASSIGNMENT IS NOT A LOSS. Put stock at a support level you chose is the \
plan working. Immediately sell calls.
- RESPECT EARNINGS AND MACRO. Be aggressive on individual names but never \
ignore the regime.

Output rules:
- Lead with the alpha: what dips/signals fired today?
- Give exact strikes, expirations, premiums, contract counts, order types
- For every opportunity: conviction level and WHY (which signals)
- Nag about idle capital and capital efficiency
- Flag ADBE concentration relentlessly
- Be blunt and direct. "Consider" means "do this."

Format the briefing with these sections in order:
1. REGIME (one word + why)
2. SIGNAL FLASH (what's on fire today)
3. ATTACK PLAN (top 3 new trades with exact sizing)
4. POSITION MANAGEMENT (closes, rolls, reloads)
5. PORTFOLIO SCORECARD (theta/day, capital efficiency, idle %, concentration)
6. ACCOUNTS & LIQUIDITY (routing, tax context)
7. TAX ALERTS (wash sales, LTCG approaching, harvest opportunities)
8. GUARDRAILS (anything that needs attention)

Use section headers like: ━━ SECTION NAME ━━
Keep each section tight — the whole briefing should be under 2500 chars.
"""


def _build_user_prompt(
    portfolio: PortfolioState,
    actions: list[tuple[Position, PositionAction, str]],
    opportunities: list[SizedOpportunity],
    risk: RiskReport,
    signals: list[AlphaSignal],
    macro: MarketContext,
) -> str:
    """Assemble the user prompt with all context sections."""
    today = date.today().strftime("%A, %B %d, %Y")
    return f"""\
Generate my morning briefing for {today}.

## Active Alpha Signals
{format_signals(signals)}

## Portfolio State
{format_portfolio(portfolio)}

## Position Actions
{format_actions(actions)}

## Sized Opportunities (ranked by composite score)
{format_opportunities(opportunities)}

## Risk Report
{format_risk(risk)}

## Macro Context
{format_macro(macro)}

Produce all 8 sections. Be specific, actionable, and under 2500 chars total."""


# ── Format helpers ──────────────────────────────────────────────


def format_signals(signals: list[AlphaSignal]) -> str:
    if not signals:
        return "No signals fired today."
    lines: list[str] = []
    for s in signals:
        lines.append(
            f"- {s.symbol}: {s.signal_type.value} "
            f"(strength {s.strength:.0f}, {s.direction}) — {s.reasoning}"
        )
    return "\n".join(lines)


def format_portfolio(portfolio: PortfolioState) -> str:
    lines = [
        f"NLV: ${portfolio.net_liquidation:,.0f}",
        f"Cash: ${portfolio.cash_available:,.0f}",
        f"Buying power: ${portfolio.buying_power:,.0f}",
        f"Daily theta: ${portfolio.portfolio_theta:,.2f}",
        f"Portfolio delta: {portfolio.portfolio_delta:,.1f}",
        f"Margin utilization: {portfolio.margin_utilization:.0%}",
        f"Positions: {len(portfolio.positions)}",
    ]
    if portfolio.concentration:
        top = sorted(
            portfolio.concentration.items(), key=lambda x: x[1], reverse=True,
        )[:5]
        conc = ", ".join(f"{sym} {pct:.1%}" for sym, pct in top)
        lines.append(f"Top concentration: {conc}")
    return "\n".join(lines)


def format_actions(actions: list[tuple[Position, PositionAction, str]]) -> str:
    if not actions:
        return "No position actions needed."
    lines: list[str] = []
    for pos, action, reason in actions:
        lines.append(
            f"- {pos.symbol} {pos.position_type} ${pos.strike}: "
            f"{action.value} — {reason}"
        )
    return "\n".join(lines)


def format_opportunities(opportunities: list[SizedOpportunity]) -> str:
    if not opportunities:
        return "No opportunities found."
    lines: list[str] = []
    for opp in opportunities[:5]:
        lines.append(
            f"- {opp.symbol}: {opp.conviction.upper()} | "
            f"SELL {opp.contracts}x ${opp.strike}P at ${opp.premium} | "
            f"{opp.annualized_yield:.0%} ann. | "
            f"${opp.capital_deployed:,.0f} deployed"
        )
    return "\n".join(lines)


def format_risk(risk: RiskReport) -> str:
    lines = [
        f"ADBE concentration: {risk.adbe_pct:.1%}",
        f"Top-5 concentration: {risk.top_5_concentration:.1%}",
        f"Portfolio beta: {risk.portfolio_beta:.2f}",
        f"Daily theta: ${risk.daily_theta:,.2f}",
        f"5% down impact: ${risk.impact_5pct_down:,.0f}",
        f"Margin utilization: {risk.margin_utilization:.0%}",
        f"Capital efficiency: {risk.capital_efficiency:.1%}",
        f"Idle capital: {risk.idle_capital_pct:.1%}",
    ]
    if risk.concentration_warnings:
        lines.append(f"Warnings: {'; '.join(risk.concentration_warnings)}")
    return "\n".join(lines)


def format_macro(macro: MarketContext) -> str:
    return (
        f"VIX: {macro.vix:.1f} ({macro.vix_change_1d:+.1f})\n"
        f"IV rank: {macro.iv_rank:.0f}\n"
        f"Price change 1d: {macro.price_change_1d:+.1f}%\n"
        f"Price: ${macro.price}"
    )
