# tests/test_intelligence.py
"""Tests for intelligence mesh models."""
from __future__ import annotations

from decimal import Decimal

from src.models.intelligence import (
    IntelligenceContext,
    OptionsIntelligence,
    PortfolioContext,
    QuantIntelligence,
    TechnicalConsensus,
)


class TestIntelligenceModels:
    def test_create_quant_intelligence(self) -> None:
        qi = QuantIntelligence(
            signals=[],
            signal_count=0,
            avg_strength=0.0,
            iv_rank=50.0,
            iv_percentile=50.0,
            rsi=45.0,
            price_vs_support={},
            trend_direction="range",
        )
        assert qi.iv_rank == 50.0

    def test_create_technical_consensus(self) -> None:
        tc = TechnicalConsensus(
            source="tradingview",
            overall="BUY",
            oscillators="NEUTRAL",
            moving_averages="BUY",
            buy_count=10,
            neutral_count=5,
            sell_count=3,
            raw_indicators={},
        )
        assert tc.overall == "BUY"

    def test_create_full_context(self) -> None:
        ctx = IntelligenceContext(
            symbol="NVDA",
            quant=QuantIntelligence(
                signals=[], signal_count=0, avg_strength=0.0,
                iv_rank=0.0, iv_percentile=0.0, rsi=50.0,
                price_vs_support={}, trend_direction="range",
            ),
            technical_consensus=None,
            options=None,
            portfolio=PortfolioContext(
                existing_exposure_pct=0.0,
                existing_positions=[],
                account_recommendation="Roth IRA",
                wash_sale_blocked=False,
                earnings_conflict=False,
                available_capital=Decimal("500000"),
            ),
            market=None,
            events=None,
        )
        assert ctx.symbol == "NVDA"
        assert ctx.technical_consensus is None

    def test_missing_sources_are_none(self) -> None:
        ctx = IntelligenceContext(
            symbol="AAPL",
            quant=QuantIntelligence(
                signals=[], signal_count=0, avg_strength=0.0,
                iv_rank=0.0, iv_percentile=0.0, rsi=50.0,
                price_vs_support={}, trend_direction="range",
            ),
            technical_consensus=None,
            options=None,
            portfolio=PortfolioContext(
                existing_exposure_pct=0.0,
                existing_positions=[],
                account_recommendation="",
                wash_sale_blocked=False,
                earnings_conflict=False,
                available_capital=Decimal("0"),
            ),
            market=None,
            events=None,
        )
        assert ctx.options is None
        assert ctx.market is None
