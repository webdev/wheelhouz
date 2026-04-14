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


import pandas as pd
from src.models.market import OptionContract, OptionsChain


class TestOptionsChain:
    def test_option_contract_creation(self) -> None:
        from datetime import date
        oc = OptionContract(
            strike=Decimal("125.00"),
            expiration=date(2026, 5, 15),
            option_type="put",
            bid=Decimal("1.45"),
            ask=Decimal("1.52"),
            mid=Decimal("1.485"),
            volume=1500,
            open_interest=8200,
            implied_vol=0.42,
            delta=-0.25,
        )
        assert oc.bid == Decimal("1.45")
        assert oc.delta == -0.25

    def test_options_chain_with_contracts(self) -> None:
        chain = OptionsChain(
            symbol="PLTR",
            puts=[],
            calls=[],
        )
        assert chain.puts == []
        assert chain.symbol == "PLTR"

    def test_fetch_options_chain_returns_populated_chain(self) -> None:
        from unittest.mock import patch, MagicMock
        from datetime import date, timedelta
        from src.data.market import fetch_options_chain

        exp_date = (date.today() + timedelta(days=30)).isoformat()
        mock_puts = pd.DataFrame({
            "strike": [120.0, 125.0, 130.0],
            "bid": [1.20, 1.80, 2.50],
            "ask": [1.30, 1.90, 2.60],
            "volume": [500, 1200, 800],
            "openInterest": [3000, 8000, 5000],
            "impliedVolatility": [0.38, 0.42, 0.45],
        })
        mock_calls = pd.DataFrame({
            "strike": [135.0, 140.0],
            "bid": [2.10, 1.50],
            "ask": [2.20, 1.60],
            "volume": [600, 400],
            "openInterest": [4000, 2000],
            "impliedVolatility": [0.35, 0.33],
        })

        mock_chain = MagicMock()
        mock_chain.puts = mock_puts
        mock_chain.calls = mock_calls

        mock_ticker = MagicMock()
        mock_ticker.options = [exp_date]
        mock_ticker.option_chain.return_value = mock_chain
        mock_ticker.history.return_value = pd.DataFrame({"Close": [131.0]})

        with patch("src.data.market.yf.Ticker", return_value=mock_ticker):
            result = fetch_options_chain("PLTR")

        assert len(result.puts) == 3
        assert len(result.calls) == 2
        assert result.puts[0].strike == Decimal("120.0")
        assert result.atm_iv is not None

    def test_fetch_options_chain_graceful_on_empty(self) -> None:
        from unittest.mock import patch, MagicMock
        from src.data.market import fetch_options_chain

        mock_ticker = MagicMock()
        mock_ticker.options = []

        with patch("src.data.market.yf.Ticker", return_value=mock_ticker):
            result = fetch_options_chain("FAKE")

        assert result.puts == []
        assert result.calls == []
        assert result.symbol == "FAKE"


from src.data.tradingview import fetch_tradingview_consensus


class TestTradingView:
    def test_fetch_returns_technical_consensus(self) -> None:
        """Should return a TechnicalConsensus with valid fields."""
        from unittest.mock import patch, MagicMock

        mock_handler = MagicMock()
        mock_handler.get_analysis.return_value = MagicMock(
            summary={"RECOMMENDATION": "BUY", "BUY": 12, "NEUTRAL": 6, "SELL": 8},
            oscillators={"RECOMMENDATION": "NEUTRAL", "BUY": 3, "NEUTRAL": 5, "SELL": 3},
            moving_averages={"RECOMMENDATION": "BUY", "BUY": 9, "NEUTRAL": 1, "SELL": 2},
            indicators={},
        )

        with patch("src.data.tradingview.TA_Handler", return_value=mock_handler):
            result = fetch_tradingview_consensus("NVDA")

        assert result is not None
        assert result.overall == "BUY"
        assert result.buy_count == 12
        assert result.source == "tradingview"

    def test_fetch_returns_none_on_failure(self) -> None:
        """Should return None gracefully on HTTP error."""
        from unittest.mock import patch

        with patch("src.data.tradingview.TA_Handler", side_effect=Exception("HTTP 429")):
            result = fetch_tradingview_consensus("FAKE")

        assert result is None

    def test_cache_returns_same_result(self) -> None:
        """Should cache results for 1 hour."""
        from unittest.mock import patch, MagicMock

        mock_handler = MagicMock()
        mock_handler.get_analysis.return_value = MagicMock(
            summary={"RECOMMENDATION": "SELL", "BUY": 4, "NEUTRAL": 5, "SELL": 17},
            oscillators={"RECOMMENDATION": "SELL", "BUY": 1, "NEUTRAL": 2, "SELL": 8},
            moving_averages={"RECOMMENDATION": "SELL", "BUY": 3, "NEUTRAL": 3, "SELL": 9},
            indicators={},
        )

        with patch("src.data.tradingview.TA_Handler", return_value=mock_handler) as mock_cls:
            from src.data.tradingview import _tv_cache
            _tv_cache.pop("CACHE_TEST_SYM", None)  # ensure clean state for this symbol
            result1 = fetch_tradingview_consensus("CACHE_TEST_SYM")
            result2 = fetch_tradingview_consensus("CACHE_TEST_SYM")

        assert result1 is not None
        assert result1.overall == result2.overall
        assert mock_cls.call_count == 1


from src.intelligence.builder import build_intelligence_context
from tests.fixtures.market_data import make_market_context, make_price_history, make_options_chain, make_event_calendar
from tests.fixtures.trades import make_alpha_signal


class TestIntelligenceBuilder:
    def test_builds_context_with_signals(self) -> None:
        from src.models.enums import SignalType
        signals = [
            make_alpha_signal(symbol="NVDA", strength=70),
            make_alpha_signal(symbol="NVDA", strength=65, signal_type=SignalType.IV_RANK_SPIKE),
        ]
        mkt = make_market_context(iv_rank=62.0)
        hist = make_price_history(rsi_14=28.0)
        chain = make_options_chain()
        cal = make_event_calendar()

        ctx = build_intelligence_context(
            symbol="NVDA",
            signals=signals,
            market=mkt,
            price_history=hist,
            chain=chain,
            calendar=cal,
        )

        assert ctx.symbol == "NVDA"
        assert ctx.quant.signal_count == 2
        assert ctx.quant.avg_strength == 67.5
        assert ctx.quant.rsi == 28.0
        assert ctx.quant.iv_rank == 62.0
        assert ctx.market is not None

    def test_builds_context_without_tradingview(self) -> None:
        """Should work when TradingView is unavailable."""
        ctx = build_intelligence_context(
            symbol="FAKE",
            signals=[],
            market=make_market_context(),
            price_history=make_price_history(),
            chain=make_options_chain(),
            calendar=make_event_calendar(),
            technical_consensus=None,
        )
        assert ctx.technical_consensus is None
        assert ctx.quant.signal_count == 0

    def test_trend_direction_from_moving_averages(self) -> None:
        """Downtrend when price below both 50 and 200 SMA."""
        hist = make_price_history(
            current_price=Decimal("130"),
            sma_50=Decimal("145"),
            sma_200=Decimal("160"),
        )
        ctx = build_intelligence_context(
            symbol="PLTR",
            signals=[],
            market=make_market_context(),
            price_history=hist,
            chain=make_options_chain(),
            calendar=make_event_calendar(),
        )
        assert ctx.quant.trend_direction == "downtrend"
