# src/intelligence/builder.py
"""Assemble IntelligenceContext from all available sources."""
from __future__ import annotations

from decimal import Decimal

from src.models.intelligence import (
    IntelligenceContext,
    OptionsIntelligence,
    PortfolioContext,
    QuantIntelligence,
    TechnicalConsensus,
)
from src.models.analysis import SmartStrike
from src.models.market import EventCalendar, MarketContext, OptionsChain, PriceHistory
from src.models.position import PortfolioState, Position
from src.models.signals import AlphaSignal


def build_intelligence_context(
    symbol: str,
    signals: list[AlphaSignal],
    market: MarketContext,
    price_history: PriceHistory,
    chain: OptionsChain,
    calendar: EventCalendar,
    technical_consensus: TechnicalConsensus | None = None,
    portfolio_state: PortfolioState | None = None,
) -> IntelligenceContext:
    """Build a unified IntelligenceContext for one symbol."""
    # Quant intelligence
    avg_strength = (
        sum(s.strength for s in signals) / len(signals) if signals else 0.0
    )
    trend = _classify_trend(price_history)
    support_distances = _calculate_support_distances(price_history)

    quant = QuantIntelligence(
        signals=signals,
        signal_count=len(signals),
        avg_strength=avg_strength,
        iv_rank=market.iv_rank,
        iv_percentile=market.iv_percentile,
        rsi=price_history.rsi_14,
        price_vs_support=support_distances,
        trend_direction=trend,
    )

    # Options intelligence
    options = None
    if chain.puts:
        best = _find_best_put(chain, price_history)
        if best:
            bid_ask_spread = (
                float(best.ask - best.bid) / float(best.mid) * 100
                if best.mid > 0 else 0.0
            )
            capital = float(best.strike) * 100
            yield_on_cap = float(best.mid) * 100 / capital if capital > 0 else 0.0
            ann_yield = yield_on_cap * (365.0 / 30.0)

            options = OptionsIntelligence(
                best_strike=SmartStrike(
                    strike=best.strike,
                    delta=best.delta,
                    premium=best.mid,
                    yield_on_capital=round(yield_on_cap, 4),
                    annualized_yield=round(ann_yield, 4),
                    technical_reason=None,
                ),
                iv_rank=market.iv_rank,
                premium_yield=round(yield_on_cap, 4),
                annualized_yield=round(ann_yield, 4),
                bid_ask_spread_pct=round(bid_ask_spread, 2),
                chain_available=True,
            )

    # Portfolio context
    existing_positions: list[Position] = []
    exposure_pct = 0.0
    available_capital = Decimal("0")
    if portfolio_state:
        existing_positions = [p for p in portfolio_state.positions if p.symbol == symbol]
        exposure_pct = portfolio_state.concentration.get(symbol, 0.0)
        available_capital = portfolio_state.buying_power

    portfolio = PortfolioContext(
        existing_exposure_pct=exposure_pct,
        existing_positions=existing_positions,
        account_recommendation="",
        wash_sale_blocked=False,
        earnings_conflict=False,
        available_capital=available_capital,
    )

    return IntelligenceContext(
        symbol=symbol,
        quant=quant,
        technical_consensus=technical_consensus,
        options=options,
        portfolio=portfolio,
        market=market,
        events=calendar,
    )


def _classify_trend(hist: PriceHistory) -> str:
    """Classify trend from SMA positions."""
    price = float(hist.current_price) if hist.current_price else 0.0
    if price <= 0:
        return "range"

    below_50 = hist.sma_50 is not None and price < float(hist.sma_50)
    below_200 = hist.sma_200 is not None and price < float(hist.sma_200)
    above_50 = hist.sma_50 is not None and price > float(hist.sma_50)
    above_200 = hist.sma_200 is not None and price > float(hist.sma_200)

    if below_50 and below_200:
        return "downtrend"
    if above_50 and above_200:
        return "uptrend"
    return "range"


def _calculate_support_distances(hist: PriceHistory) -> dict[str, float]:
    """Calculate % distance from current price to each support level."""
    price = float(hist.current_price) if hist.current_price else 0.0
    if price <= 0:
        return {}

    distances: dict[str, float] = {}
    if hist.sma_200:
        distances["200 SMA"] = round((price - float(hist.sma_200)) / price * 100, 1)
    if hist.sma_50:
        distances["50 SMA"] = round((price - float(hist.sma_50)) / price * 100, 1)
    if hist.low_52w:
        distances["52w Low"] = round((price - float(hist.low_52w)) / price * 100, 1)
    return distances


def _find_best_put(chain: OptionsChain, hist: PriceHistory) -> object | None:
    """Find the best OTM put from the chain (nearest to 0.25-0.30 delta range)."""
    if not chain.puts:
        return None

    price = float(hist.current_price) if hist.current_price else 0.0
    if price <= 0:
        return None

    otm_puts = [p for p in chain.puts if float(p.strike) < price and p.bid > 0]
    if not otm_puts:
        return None

    target_strike = price * 0.93
    return min(otm_puts, key=lambda p: abs(float(p.strike) - target_strike))
