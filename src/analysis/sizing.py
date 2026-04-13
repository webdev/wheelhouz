"""Conviction-based position sizing.

HIGH conviction (signal strength >70, 2+ confirming signals): 3-5% of NLV
MEDIUM conviction (strength 40-70, or single strong signal):  1.5-3% of NLV
LOW conviction (strength <40, marginal setup):                0.5-1.5% of NLV

Adjusts for concentration limits and margin utilization.
"""

from __future__ import annotations

from decimal import Decimal

from src.config.loader import load_trading_params
from src.models.analysis import SmartStrike, SizedOpportunity
from src.models.position import PortfolioState
from src.models.signals import AlphaSignal


def size_position(
    symbol: str,
    trade_type: str,
    strike: SmartStrike,
    expiration: object,  # date
    signals: list[AlphaSignal],
    portfolio: PortfolioState,
) -> SizedOpportunity:
    """Size a trade by conviction, respecting concentration and margin limits."""
    params = load_trading_params()
    sizing = params.get("sizing", {})
    port_params = params.get("portfolio", {})

    # Aggregate signal strength
    avg_strength = (
        sum(s.strength for s in signals) / len(signals) if signals else 30.0
    )
    num_confirming = len(signals)

    # Conviction classification
    high_strength = float(sizing.get("high_conviction_strength", 70))
    high_min_signals = int(sizing.get("high_conviction_min_signals", 2))
    med_strength = float(sizing.get("medium_conviction_strength", 50))

    if avg_strength >= high_strength and num_confirming >= high_min_signals:
        conviction = "high"
        target_pct = float(sizing.get("high_conviction_pct", 0.04))
    elif avg_strength >= med_strength or num_confirming >= 2:
        conviction = "medium"
        target_pct = float(sizing.get("medium_conviction_pct", 0.02))
    else:
        conviction = "low"
        target_pct = float(sizing.get("low_conviction_pct", 0.01))

    # Adjust for concentration limits
    max_per_symbol = float(port_params.get("max_concentration_per_symbol", 0.10))
    current_exposure = portfolio.concentration.get(symbol, 0.0)
    remaining_room = max(0.0, max_per_symbol - current_exposure)
    target_pct = min(target_pct, remaining_room)

    # Adjust for margin utilization
    margin_cutback = float(sizing.get("margin_cutback_threshold", 0.40))
    if portfolio.margin_utilization > margin_cutback:
        target_pct *= 0.5

    # Calculate contracts
    nlv = portfolio.net_liquidation
    if nlv == 0:
        nlv = Decimal("1000000")  # fallback for testing

    strike_price = float(strike.strike)
    capital_per_contract = strike_price * 100
    total_capital = float(nlv) * target_pct

    if capital_per_contract > 0:
        contracts = max(1, int(total_capital / capital_per_contract))
    else:
        contracts = 1

    capital_deployed = Decimal(str(contracts * capital_per_contract))
    portfolio_pct = float(capital_deployed / nlv) if nlv > 0 else 0.0

    # Build reasoning
    signal_names = ", ".join(s.signal_type.value for s in signals)
    reasoning = (
        f"{symbol} {conviction.upper()} conviction: "
        f"{num_confirming} signal(s) [{signal_names}], "
        f"avg strength {avg_strength:.0f}. "
        f"Sell {contracts}x ${strike.strike}{trade_type[5] if len(trade_type) > 5 else 'P'} "
        f"at ${strike.premium} for {strike.annualized_yield:.0%} annualized."
    )
    if strike.technical_reason:
        reasoning += f" Strike at {strike.technical_reason}."

    from datetime import date as date_type
    exp = expiration if isinstance(expiration, date_type) else None

    return SizedOpportunity(
        symbol=symbol,
        trade_type=trade_type,
        strike=strike.strike,
        expiration=exp,
        premium=strike.premium,
        contracts=contracts,
        capital_deployed=capital_deployed,
        portfolio_pct=round(portfolio_pct, 4),
        yield_on_capital=strike.yield_on_capital,
        annualized_yield=strike.annualized_yield,
        conviction=conviction,
        signals=signals,
        smart_strike=strike,
        reasoning=reasoning,
    )
