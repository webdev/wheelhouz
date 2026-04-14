# tests/fixtures/intelligence.py
"""Factory functions for IntelligenceContext and sub-models."""
from __future__ import annotations

from decimal import Decimal

from src.models.intelligence import (
    IntelligenceContext,
    OptionsIntelligence,
    PortfolioContext,
    QuantIntelligence,
    TechnicalConsensus,
)


def make_quant_intelligence(**overrides) -> QuantIntelligence:
    defaults = dict(
        signals=[], signal_count=0, avg_strength=0.0,
        iv_rank=50.0, iv_percentile=50.0, rsi=45.0,
        price_vs_support={}, trend_direction="range",
    )
    defaults.update(overrides)
    return QuantIntelligence(**defaults)


def make_technical_consensus(**overrides) -> TechnicalConsensus:
    defaults = dict(
        source="tradingview", overall="NEUTRAL",
        oscillators="NEUTRAL", moving_averages="NEUTRAL",
        buy_count=8, neutral_count=8, sell_count=8,
        raw_indicators={},
    )
    defaults.update(overrides)
    return TechnicalConsensus(**defaults)


def make_portfolio_context(**overrides) -> PortfolioContext:
    defaults = dict(
        existing_exposure_pct=0.0, existing_positions=[],
        account_recommendation="Roth IRA", wash_sale_blocked=False,
        earnings_conflict=False, available_capital=Decimal("500000"),
    )
    defaults.update(overrides)
    return PortfolioContext(**defaults)


def make_intelligence_context(**overrides) -> IntelligenceContext:
    defaults = dict(
        symbol="NVDA",
        quant=make_quant_intelligence(),
        technical_consensus=None,
        options=None,
        portfolio=make_portfolio_context(),
        market=None,
        events=None,
    )
    defaults.update(overrides)
    return IntelligenceContext(**defaults)
