# tests/test_intelligence.py
"""Tests for intelligence mesh models."""
from __future__ import annotations

from datetime import date
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


from src.delivery.reasoning import build_reasoning_prompt


class TestClaudeReasoning:
    def test_build_prompt_includes_all_sections(self) -> None:
        from tests.fixtures.intelligence import (
            make_intelligence_context,
            make_quant_intelligence,
            make_technical_consensus,
        )
        ctx = make_intelligence_context(
            quant=make_quant_intelligence(signal_count=2, avg_strength=65, rsi=28.0),
            technical_consensus=make_technical_consensus(overall="SELL"),
        )
        prompt = build_reasoning_prompt([ctx])
        assert "NVDA" in prompt
        assert "QUANT SIGNALS" in prompt
        assert "TRADINGVIEW" in prompt
        assert "SELL" in prompt

    def test_build_prompt_handles_missing_tradingview(self) -> None:
        from tests.fixtures.intelligence import make_intelligence_context
        ctx = make_intelligence_context(technical_consensus=None)
        prompt = build_reasoning_prompt([ctx])
        assert "TRADINGVIEW: unavailable" in prompt

    def test_build_prompt_caps_at_5_symbols(self) -> None:
        from tests.fixtures.intelligence import make_intelligence_context
        contexts = [make_intelligence_context(symbol=f"SYM{i}") for i in range(8)]
        prompt = build_reasoning_prompt(contexts)
        assert "SYM0" in prompt
        assert "SYM4" in prompt
        assert "SYM5" not in prompt


class TestBriefingWiring:
    def test_format_local_briefing_accepts_intel_contexts(self) -> None:
        """format_local_briefing should accept and render intel_contexts."""
        from datetime import datetime
        from src.main import format_local_briefing
        from src.monitor.regime import RegimeState
        from tests.fixtures.intelligence import make_intelligence_context, make_technical_consensus

        regime = RegimeState(
            regime="hold", vix=19.0, spy_change_pct=0.005,
            severity="normal", target_deployed=0.70, timestamp=datetime.utcnow(),
        )
        ctx = make_intelligence_context(
            symbol="NVDA",
            technical_consensus=make_technical_consensus(overall="BUY"),
        )

        # Minimal valid call with the new parameters
        briefing = format_local_briefing(
            regime=regime,
            vix=19.0,
            spy_change=0.005,
            all_signals=[],
            watchlist_data=[],
            tax_alerts=[],
            recommendations=None,
            intel_contexts=[ctx],
            analyst_brief="Test analyst brief content",
        )

        assert "WHEEL COPILOT" in briefing
        assert "ANALYST BRIEF" in briefing
        assert "Test analyst brief content" in briefing

    def test_format_local_briefing_works_without_intel(self) -> None:
        """format_local_briefing should work with no intel contexts (backward compat)."""
        from datetime import datetime
        from src.main import format_local_briefing
        from src.monitor.regime import RegimeState

        regime = RegimeState(
            regime="attack", vix=15.0, spy_change_pct=0.01,
            severity="normal", target_deployed=0.70, timestamp=datetime.utcnow(),
        )
        briefing = format_local_briefing(
            regime=regime,
            vix=15.0,
            spy_change=0.01,
            all_signals=[],
            watchlist_data=[],
            tax_alerts=[],
        )

        assert "WHEEL COPILOT" in briefing
        assert "TRADINGVIEW CONSENSUS" not in briefing


class TestPortfolioLoading:
    def test_alpaca_position_to_position(self) -> None:
        """Convert an AlpacaPosition to the shared Position model."""
        from src.data.portfolio import alpaca_position_to_position
        from src.execution.alpaca_client import AlpacaPosition

        ap = AlpacaPosition(
            symbol="PLTR260515P00125000",
            quantity=-1,
            avg_entry_price=Decimal("1.80"),
            current_price=Decimal("2.65"),
            unrealized_pnl=Decimal("-85"),
            market_value=Decimal("265"),
        )
        pos = alpaca_position_to_position(ap)

        assert pos.symbol == "PLTR"
        assert pos.position_type == "short_put"
        assert pos.strike == Decimal("125")
        assert pos.entry_price == Decimal("1.80")
        assert pos.days_to_expiry >= 0

    def test_load_portfolio_state_from_alpaca(self) -> None:
        """load_portfolio_state should return PortfolioState with converted positions."""
        from unittest.mock import patch, MagicMock
        from src.data.portfolio import load_portfolio_state
        from src.execution.alpaca_client import AlpacaPosition, AlpacaAccountInfo

        mock_account = AlpacaAccountInfo(
            equity=Decimal("500000"),
            buying_power=Decimal("250000"),
            cash=Decimal("150000"),
            portfolio_value=Decimal("500000"),
            positions=[
                AlpacaPosition(
                    symbol="NVDA260515P00130000",
                    quantity=-2,
                    avg_entry_price=Decimal("3.20"),
                    current_price=Decimal("2.50"),
                    unrealized_pnl=Decimal("140"),
                    market_value=Decimal("500"),
                ),
            ],
        )

        mock_client = MagicMock()
        mock_client.get_account.return_value = mock_account
        from pathlib import Path
        with patch("src.data.portfolio.PORTFOLIO_YAML", Path("/nonexistent/portfolio.yaml")), \
             patch("src.data.auth.get_session", side_effect=Exception("no etrade")), \
             patch("src.data.portfolio.AlpacaPaperClient", return_value=mock_client):
            state = load_portfolio_state()

        assert state.buying_power == Decimal("250000")
        assert len(state.positions) == 1
        assert state.positions[0].symbol == "NVDA"
        assert state.concentration.get("NVDA", 0) > 0

    def test_load_portfolio_state_empty(self) -> None:
        """load_portfolio_state returns empty state when no positions."""
        from unittest.mock import patch, MagicMock
        from src.data.portfolio import load_portfolio_state
        from src.execution.alpaca_client import AlpacaAccountInfo

        mock_account = AlpacaAccountInfo(
            equity=Decimal("500000"),
            buying_power=Decimal("500000"),
        )

        mock_client = MagicMock()
        mock_client.get_account.return_value = mock_account
        from pathlib import Path
        with patch("src.data.portfolio.PORTFOLIO_YAML", Path("/nonexistent/portfolio.yaml")), \
             patch("src.data.auth.get_session", side_effect=Exception("no etrade")), \
             patch("src.data.portfolio.AlpacaPaperClient", return_value=mock_client):
            state = load_portfolio_state()

        assert state.positions == []
        assert state.buying_power == Decimal("500000")


class TestPositionReview:
    def test_close_now_when_loss_stop_hit(self) -> None:
        """Should recommend CLOSE NOW when loss exceeds 2x premium."""
        from src.intelligence.position_review import review_position
        from tests.fixtures.intelligence import make_intelligence_context, make_quant_intelligence
        from src.models.position import Position

        pos = Position(
            symbol="PLTR", position_type="short_put", quantity=1,
            strike=Decimal("125"), expiration=date(2026, 5, 15),
            entry_price=Decimal("1.80"), current_price=Decimal("4.50"),
            underlying_price=Decimal("120"), cost_basis=Decimal("12500"),
            delta=-0.45, theta=0.03, gamma=0.01, vega=0.08, iv=0.55,
            days_to_expiry=32, unrealized_pnl=Decimal("-270"),
        )
        ctx = make_intelligence_context(
            symbol="PLTR",
            quant=make_quant_intelligence(trend_direction="downtrend"),
        )

        result = review_position(pos, ctx)
        assert result.action == "CLOSE NOW"
        assert "loss stop" in result.reasoning.lower()

    def test_hold_when_thesis_intact(self) -> None:
        """Should recommend HOLD when position is healthy."""
        from src.intelligence.position_review import review_position
        from tests.fixtures.intelligence import (
            make_intelligence_context, make_quant_intelligence, make_technical_consensus,
        )
        from src.models.position import Position

        pos = Position(
            symbol="NVDA", position_type="short_put", quantity=2,
            strike=Decimal("130"), expiration=date(2026, 5, 15),
            entry_price=Decimal("3.20"), current_price=Decimal("1.50"),
            underlying_price=Decimal("145"), cost_basis=Decimal("26000"),
            delta=-0.15, theta=0.05, gamma=0.01, vega=0.06, iv=0.40,
            days_to_expiry=32, unrealized_pnl=Decimal("340"),
        )
        ctx = make_intelligence_context(
            symbol="NVDA",
            quant=make_quant_intelligence(trend_direction="uptrend"),
            technical_consensus=make_technical_consensus(overall="BUY"),
        )

        result = review_position(pos, ctx)
        assert result.action == "HOLD"

    def test_take_profit_when_most_of_premium_captured(self) -> None:
        """Should recommend TAKE PROFIT when >75% of premium captured."""
        from src.intelligence.position_review import review_position
        from tests.fixtures.intelligence import make_intelligence_context, make_quant_intelligence
        from src.models.position import Position

        pos = Position(
            symbol="AAPL", position_type="short_put", quantity=1,
            strike=Decimal("200"), expiration=date(2026, 5, 15),
            entry_price=Decimal("4.00"), current_price=Decimal("0.80"),
            underlying_price=Decimal("215"), cost_basis=Decimal("20000"),
            delta=-0.08, theta=0.02, gamma=0.005, vega=0.03, iv=0.25,
            days_to_expiry=32, unrealized_pnl=Decimal("320"),
        )
        ctx = make_intelligence_context(
            symbol="AAPL",
            quant=make_quant_intelligence(trend_direction="uptrend"),
        )

        result = review_position(pos, ctx)
        assert result.action == "TAKE PROFIT"

    def test_watch_closely_when_tv_flips(self) -> None:
        """Should recommend WATCH CLOSELY when TradingView flips bearish."""
        from src.intelligence.position_review import review_position
        from tests.fixtures.intelligence import (
            make_intelligence_context, make_quant_intelligence, make_technical_consensus,
        )
        from src.models.position import Position

        pos = Position(
            symbol="META", position_type="short_put", quantity=1,
            strike=Decimal("450"), expiration=date(2026, 5, 15),
            entry_price=Decimal("6.50"), current_price=Decimal("5.00"),
            underlying_price=Decimal("460"), cost_basis=Decimal("45000"),
            delta=-0.30, theta=0.04, gamma=0.01, vega=0.10, iv=0.45,
            days_to_expiry=32, unrealized_pnl=Decimal("150"),
        )
        ctx = make_intelligence_context(
            symbol="META",
            quant=make_quant_intelligence(trend_direction="range"),
            technical_consensus=make_technical_consensus(overall="STRONG_SELL"),
        )

        result = review_position(pos, ctx)
        assert result.action == "WATCH CLOSELY"


class TestPositionReviewBriefing:
    def test_position_review_renders_in_briefing(self) -> None:
        """format_local_briefing should render POSITION REVIEW when reviews provided."""
        from datetime import datetime
        from src.main import format_local_briefing
        from src.monitor.regime import RegimeState
        from src.intelligence.position_review import PositionReview

        regime = RegimeState(
            regime="hold", vix=19.0, spy_change_pct=0.005,
            severity="normal", target_deployed=0.70, timestamp=datetime.utcnow(),
        )
        reviews = [
            PositionReview(
                symbol="PLTR", action="CLOSE NOW",
                reasoning="Loss stop hit: current $4.50 is 2.5x entry $1.80",
                current_pnl=Decimal("-270"), days_to_expiry=32,
            ),
            PositionReview(
                symbol="NVDA", action="HOLD",
                reasoning="Thesis intact. TradingView: BUY. Trend: uptrend",
                current_pnl=Decimal("340"), days_to_expiry=32,
            ),
        ]

        briefing = format_local_briefing(
            regime=regime,
            vix=19.0,
            spy_change=0.005,
            all_signals=[],
            watchlist_data=[],
            tax_alerts=[],
            position_reviews=reviews,
        )

        assert "DO NOW" in briefing
        assert "PLTR" in briefing
        assert "CLOSE NOW" in briefing
        assert "HOLD" in briefing


class TestIntegrationPipeline:
    def test_full_pipeline_with_mock_data(self) -> None:
        """End-to-end: signals + TV consensus + builder → IntelligenceContext."""
        from src.analysis.signals import detect_all_signals
        from src.intelligence.builder import build_intelligence_context
        from src.delivery.reasoning import build_reasoning_prompt
        from tests.fixtures.market_data import (
            make_market_context, make_price_history,
            make_options_chain, make_event_calendar,
        )
        from tests.fixtures.intelligence import make_technical_consensus

        mkt = make_market_context(price_change_1d=-3.5, iv_rank=70)
        hist = make_price_history(rsi_14=28.0)
        chain = make_options_chain()
        cal = make_event_calendar()

        signals = detect_all_signals("NVDA", mkt, hist, chain, cal)

        tv = make_technical_consensus(
            overall="SELL",
            moving_averages="STRONG_SELL",
            buy_count=4, neutral_count=5, sell_count=17,
        )

        ctx = build_intelligence_context(
            symbol="NVDA",
            signals=signals,
            market=mkt,
            price_history=hist,
            chain=chain,
            calendar=cal,
            technical_consensus=tv,
        )

        assert ctx.symbol == "NVDA"
        assert ctx.quant.signal_count >= 1
        assert ctx.technical_consensus.overall == "SELL"
        assert ctx.quant.trend_direction in ("uptrend", "downtrend", "range")

        # Build reasoning prompt
        prompt = build_reasoning_prompt([ctx])
        assert "NVDA" in prompt
        assert "SELL" in prompt
        assert "QUANT SIGNALS" in prompt

    def test_pipeline_graceful_without_tradingview(self) -> None:
        """Pipeline works when TradingView is None."""
        from src.intelligence.builder import build_intelligence_context
        from src.delivery.reasoning import build_reasoning_prompt
        from tests.fixtures.market_data import (
            make_market_context, make_price_history,
            make_options_chain, make_event_calendar,
        )

        ctx = build_intelligence_context(
            symbol="AAPL",
            signals=[],
            market=make_market_context(),
            price_history=make_price_history(),
            chain=make_options_chain(),
            calendar=make_event_calendar(),
            technical_consensus=None,
        )

        prompt = build_reasoning_prompt([ctx])
        assert "AAPL" in prompt
        assert "TRADINGVIEW: unavailable" in prompt


class TestTVConvictionAdjustment:
    def test_tv_strong_sell_forces_skip(self) -> None:
        """STRONG_SELL always forces SKIP regardless of starting conviction."""
        from src.main import _apply_tv_adjustment
        from src.models.analysis import SizedOpportunity

        sized = SizedOpportunity(
            symbol="PLTR", trade_type="sell_put", strike=Decimal("125"),
            expiration=None, premium=Decimal("1.80"), contracts=1,
            capital_deployed=Decimal("12500"), portfolio_pct=0.012,
            yield_on_capital=0.014, annualized_yield=0.17,
            conviction="high", reasoning="test",
        )
        result = _apply_tv_adjustment(sized, "STRONG_SELL")
        assert result.conviction == "skip"
        assert "downgraded" in result.reasoning

    def test_tv_sell_caps_at_low(self) -> None:
        """SELL caps conviction at LOW — never actionable, watch list only."""
        from src.main import _apply_tv_adjustment
        from src.models.analysis import SizedOpportunity

        sized = SizedOpportunity(
            symbol="ADBE", trade_type="sell_put", strike=Decimal("400"),
            expiration=None, premium=Decimal("5.00"), contracts=1,
            capital_deployed=Decimal("40000"), portfolio_pct=0.04,
            yield_on_capital=0.012, annualized_yield=0.15,
            conviction="high", reasoning="test",
        )
        result = _apply_tv_adjustment(sized, "SELL")
        assert result.conviction == "low"
        assert "downgraded" in result.reasoning

    def test_tv_buy_upgrades_conviction(self) -> None:
        """BUY consensus should upgrade LOW → MEDIUM."""
        from src.main import _apply_tv_adjustment
        from src.models.analysis import SizedOpportunity

        sized = SizedOpportunity(
            symbol="NVDA", trade_type="sell_put", strike=Decimal("130"),
            expiration=None, premium=Decimal("3.20"), contracts=2,
            capital_deployed=Decimal("26000"), portfolio_pct=0.026,
            yield_on_capital=0.025, annualized_yield=0.30,
            conviction="low", reasoning="test",
        )
        result = _apply_tv_adjustment(sized, "STRONG_BUY")
        assert result.conviction == "medium"
        assert "upgraded" in result.reasoning

    def test_tv_sell_keeps_low_at_low(self) -> None:
        """SELL on LOW conviction → stays LOW (already at cap)."""
        from src.main import _apply_tv_adjustment
        from src.models.analysis import SizedOpportunity

        sized = SizedOpportunity(
            symbol="CRM", trade_type="sell_put", strike=Decimal("170"),
            expiration=None, premium=Decimal("2.50"), contracts=1,
            capital_deployed=Decimal("17000"), portfolio_pct=0.017,
            yield_on_capital=0.015, annualized_yield=0.18,
            conviction="low", reasoning="test",
        )
        result = _apply_tv_adjustment(sized, "SELL")
        assert result.conviction == "low"  # already at cap, no change

    def test_tv_neutral_no_change(self) -> None:
        """NEUTRAL consensus should not change conviction."""
        from src.main import _apply_tv_adjustment
        from src.models.analysis import SizedOpportunity

        sized = SizedOpportunity(
            symbol="AAPL", trade_type="sell_put", strike=Decimal("200"),
            expiration=None, premium=Decimal("4.00"), contracts=1,
            capital_deployed=Decimal("20000"), portfolio_pct=0.02,
            yield_on_capital=0.02, annualized_yield=0.24,
            conviction="medium", reasoning="test",
        )
        result = _apply_tv_adjustment(sized, "NEUTRAL")
        assert result.conviction == "medium"
        assert result.reasoning == "test"
