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


from unittest.mock import patch, MagicMock
import pandas as pd
import numpy as np


class TestIVRankFix:
    def test_iv_rank_uses_hv_fallback_when_no_current_iv(self) -> None:
        """calculate_iv_rank should use HV as proxy when current_iv=0."""
        from src.data.market import calculate_iv_rank

        dates = pd.date_range("2025-04-01", periods=252, freq="B")
        np.random.seed(42)
        returns = np.random.normal(0.0005, 0.015, 252)
        prices = 100 * np.exp(np.cumsum(returns))
        hist = pd.DataFrame({"Close": prices}, index=dates)

        with patch("yfinance.Ticker") as mock_ticker:
            mock_ticker.return_value.history.return_value = hist
            result = calculate_iv_rank("TEST", current_iv=0.0)

        assert result["iv_rank"] != 0.0
        assert result["hv_30d"] > 0.0
        assert 0.0 <= result["iv_rank"] <= 100.0

    def test_market_context_always_has_iv_rank(self) -> None:
        """fetch_market_context should never return iv_rank=0 for valid symbols."""
        from src.data.market import fetch_market_context

        dates = pd.date_range("2025-04-07", periods=5, freq="B")
        prices = [130.0, 128.0, 132.0, 131.0, 133.0]
        hist_5d = pd.DataFrame({"Close": prices}, index=dates)

        dates_1y = pd.date_range("2025-04-01", periods=252, freq="B")
        np.random.seed(42)
        returns = np.random.normal(0.0005, 0.015, 252)
        prices_1y = 130 * np.exp(np.cumsum(returns))
        hist_1y = pd.DataFrame({"Close": prices_1y}, index=dates_1y)

        with patch("yfinance.Ticker") as mock_ticker:
            instance = MagicMock()
            instance.history.side_effect = [hist_5d, hist_1y, hist_1y]
            mock_ticker.return_value = instance

            with patch("src.data.market.yf.Ticker") as mock_yf:
                mock_yf.return_value = instance
                mkt = fetch_market_context("PLTR", current_iv=0.0)

        assert mkt.iv_rank > 0.0
