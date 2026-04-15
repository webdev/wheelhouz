# tests/test_intelligence.py
"""Tests for intelligence mesh models."""
from __future__ import annotations

from datetime import date, datetime, timedelta
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

        with patch("src.data.tradingview.TA_Handler", return_value=mock_handler) as mock_cls, \
             patch("src.data.tradingview._read_disk_cache", return_value=None), \
             patch("src.data.tradingview._write_disk_cache"):
            from src.data.tradingview import _mem_cache
            _mem_cache.pop("CACHE_TEST_SYM", None)  # ensure clean state for this symbol
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
            entry_price=Decimal("3.20"), current_price=Decimal("2.00"),
            underlying_price=Decimal("145"), cost_basis=Decimal("26000"),
            delta=-0.15, theta=0.05, gamma=0.01, vega=0.06, iv=0.40,
            days_to_expiry=32, unrealized_pnl=Decimal("240"),
        )
        ctx = make_intelligence_context(
            symbol="NVDA",
            quant=make_quant_intelligence(trend_direction="uptrend"),
            technical_consensus=make_technical_consensus(overall="BUY"),
        )

        result = review_position(pos, ctx)
        assert result.action == "HOLD"

    def test_take_profit_when_most_of_premium_captured(self) -> None:
        """Should recommend TAKE PROFIT when >50% of premium captured."""
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

    def test_deep_otm_no_take_profit_at_56pct(self) -> None:
        """Deep OTM (delta < 0.10) should NOT take profit at 56% — let it ride."""
        from src.intelligence.position_review import review_position
        from tests.fixtures.intelligence import make_intelligence_context, make_quant_intelligence
        from src.models.position import Position

        # Mirrors the GOOG $270 put scenario: 18% OTM, 56% captured
        pos = Position(
            symbol="GOOG", position_type="short_put", quantity=1,
            strike=Decimal("270"), expiration=date(2026, 10, 16),
            entry_price=Decimal("2.00"), current_price=Decimal("0.88"),
            underlying_price=Decimal("330"), cost_basis=Decimal("27000"),
            delta=-0.05, theta=0.01, gamma=0.002, vega=0.02, iv=0.25,
            days_to_expiry=185, unrealized_pnl=Decimal("112"),
        )
        ctx = make_intelligence_context(
            symbol="GOOG",
            quant=make_quant_intelligence(trend_direction="uptrend"),
        )

        result = review_position(pos, ctx)
        assert result.action == "HOLD", (
            f"Deep OTM put at 56% captured should HOLD, not {result.action}"
        )

    def test_deep_otm_takes_profit_at_80pct(self) -> None:
        """Deep OTM (delta < 0.10) SHOULD take profit once 80%+ captured."""
        from src.intelligence.position_review import review_position
        from tests.fixtures.intelligence import make_intelligence_context, make_quant_intelligence
        from src.models.position import Position

        pos = Position(
            symbol="GOOG", position_type="short_put", quantity=1,
            strike=Decimal("270"), expiration=date(2026, 10, 16),
            entry_price=Decimal("2.00"), current_price=Decimal("0.35"),
            underlying_price=Decimal("330"), cost_basis=Decimal("27000"),
            delta=-0.03, theta=0.005, gamma=0.001, vega=0.01, iv=0.20,
            days_to_expiry=185, unrealized_pnl=Decimal("165"),
        )
        ctx = make_intelligence_context(
            symbol="GOOG",
            quant=make_quant_intelligence(trend_direction="uptrend"),
        )

        result = review_position(pos, ctx)
        assert result.action == "TAKE PROFIT", (
            f"Deep OTM at 82% captured should TAKE PROFIT, not {result.action}"
        )

    def test_near_atm_takes_profit_early(self) -> None:
        """Near ATM (delta > 0.25) should take profit at 40% — real risk."""
        from src.intelligence.position_review import review_position
        from tests.fixtures.intelligence import make_intelligence_context, make_quant_intelligence
        from src.models.position import Position

        pos = Position(
            symbol="META", position_type="short_put", quantity=1,
            strike=Decimal("580"), expiration=date(2026, 5, 15),
            entry_price=Decimal("8.00"), current_price=Decimal("4.70"),
            underlying_price=Decimal("585"), cost_basis=Decimal("58000"),
            delta=-0.35, theta=0.06, gamma=0.015, vega=0.12, iv=0.50,
            days_to_expiry=32, unrealized_pnl=Decimal("330"),
        )
        ctx = make_intelligence_context(
            symbol="META",
            quant=make_quant_intelligence(trend_direction="range"),
        )

        result = review_position(pos, ctx)
        assert result.action == "TAKE PROFIT", (
            f"Near ATM at 41% captured should TAKE PROFIT, not {result.action}"
        )

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


class TestCallSignals:
    """Signal-driven covered call detection."""

    def test_overbought_rsi_fires(self) -> None:
        """RSI > 70 should fire overbought signal with sell_call direction."""
        from src.analysis.signals import detect_overbought_rsi
        from tests.fixtures.market_data import make_price_history

        hist = make_price_history(rsi_14=75.0)
        result = detect_overbought_rsi("AAPL", hist)

        assert result is not None
        assert result.direction == "sell_call"
        assert result.signal_type.value == "overbought_rsi"
        assert "overbought" in result.reasoning.lower()

    def test_overbought_rsi_silent_when_normal(self) -> None:
        """RSI at 55 should not fire."""
        from src.analysis.signals import detect_overbought_rsi
        from tests.fixtures.market_data import make_price_history

        hist = make_price_history(rsi_14=55.0)
        assert detect_overbought_rsi("AAPL", hist) is None

    def test_resistance_test_fires_near_52w_high(self) -> None:
        """Price within 3% of 52-week high should fire resistance signal."""
        from src.analysis.signals import detect_resistance_test
        from tests.fixtures.market_data import make_market_context, make_price_history

        # Price $970, 52w high $995 → 2.5% below → should fire
        mkt = make_market_context(price=Decimal("970.00"))
        hist = make_price_history(current_price=Decimal("970.00"), high_52w=Decimal("995.00"))
        result = detect_resistance_test("NVDA", mkt, hist)

        assert result is not None
        assert result.direction == "sell_call"
        assert "52w high" in result.reasoning

    def test_resistance_test_silent_when_far(self) -> None:
        """Price 15% below 52w high with no nearby SMA resistance should not fire."""
        from src.analysis.signals import detect_resistance_test
        from tests.fixtures.market_data import make_market_context, make_price_history

        # All SMAs below price, 52w high far away
        mkt = make_market_context(price=Decimal("850.00"))
        hist = make_price_history(
            current_price=Decimal("850.00"), high_52w=Decimal("995.00"),
            sma_200=Decimal("820.00"), sma_50=Decimal("840.00"),
        )
        assert detect_resistance_test("NVDA", mkt, hist) is None

    def test_multi_day_rally_fires(self) -> None:
        """3+ green days with 5%+ rally should fire."""
        from src.analysis.signals import detect_multi_day_rally
        from tests.fixtures.market_data import make_price_history

        # Build closes that show a clear 3-day rally from a recent low
        closes = [Decimal("100"), Decimal("98"), Decimal("95"),  # dip
                  Decimal("98"), Decimal("101"), Decimal("104")]  # 3 green days, ~9.5% from low
        hist = make_price_history(
            current_price=Decimal("104"),
            daily_closes=closes,
        )
        result = detect_multi_day_rally("AAPL", hist)

        assert result is not None
        assert result.direction == "sell_call"
        assert "green days" in result.reasoning

    def test_multi_day_rally_silent_on_flat(self) -> None:
        """Flat price action should not fire."""
        from src.analysis.signals import detect_multi_day_rally
        from tests.fixtures.market_data import make_price_history

        closes = [Decimal("100"), Decimal("100.5"), Decimal("100.2"),
                  Decimal("100.3"), Decimal("100.1")]
        hist = make_price_history(current_price=Decimal("100.1"), daily_closes=closes)
        assert detect_multi_day_rally("AAPL", hist) is None

    def test_volume_climax_up_fires(self) -> None:
        """3x+ volume on an up day should fire."""
        from src.analysis.signals import detect_volume_climax_up
        from tests.fixtures.market_data import make_market_context, make_price_history

        mkt = make_market_context(price_change_1d=3.5)  # up day
        vols = [50_000_000.0] * 19 + [200_000_000.0]  # last day is 4x average
        hist = make_price_history(daily_volumes=vols)
        result = detect_volume_climax_up("NVDA", mkt, hist)

        assert result is not None
        assert result.direction == "sell_call"
        assert "exhaustion" in result.reasoning.lower()

    def test_volume_climax_up_silent_on_down_day(self) -> None:
        """High volume on a down day should NOT fire (that's a put signal)."""
        from src.analysis.signals import detect_volume_climax_up
        from tests.fixtures.market_data import make_market_context, make_price_history

        mkt = make_market_context(price_change_1d=-3.5)  # down day
        vols = [50_000_000.0] * 19 + [200_000_000.0]
        hist = make_price_history(daily_volumes=vols)
        assert detect_volume_climax_up("NVDA", mkt, hist) is None

    def test_call_signals_in_detect_all(self) -> None:
        """detect_all_signals should include call signals when conditions are met."""
        from src.analysis.signals import detect_all_signals
        from tests.fixtures.market_data import (
            make_market_context, make_price_history,
            make_options_chain, make_event_calendar,
        )

        # RSI 75 should fire overbought_rsi
        mkt = make_market_context(price_change_1d=1.0)
        hist = make_price_history(rsi_14=75.0)
        chain = make_options_chain()
        cal = make_event_calendar()

        signals = detect_all_signals("AAPL", mkt, hist, chain, cal)
        call_signals = [s for s in signals if s.direction == "sell_call"]
        assert len(call_signals) >= 1
        assert any(s.signal_type.value == "overbought_rsi" for s in call_signals)

    def test_call_signals_only_recommend_on_owned_stock(self) -> None:
        """build_recommendations should skip call signals when stock is not owned."""
        from src.main import build_recommendations
        from tests.fixtures.market_data import (
            make_market_context, make_price_history,
            make_options_chain, make_event_calendar,
        )
        from src.models.signals import AlphaSignal
        from src.models.enums import SignalType
        from src.models.position import PortfolioState

        call_signal = AlphaSignal(
            symbol="AAPL",
            signal_type=SignalType.OVERBOUGHT_RSI,
            strength=70.0,
            direction="sell_call",
            reasoning="test",
            expires=datetime.utcnow() + timedelta(hours=24),
        )

        mkt = make_market_context(symbol="AAPL", price=Decimal("200.00"))
        hist = make_price_history(symbol="AAPL", current_price=Decimal("200.00"))
        chain = make_options_chain(symbol="AAPL")
        cal = make_event_calendar(symbol="AAPL")
        watchlist_data = [("AAPL", mkt, hist, chain, cal)]

        # No positions = no owned stock = no call recs
        portfolio = PortfolioState()
        recs = build_recommendations([call_signal], watchlist_data, portfolio=portfolio)
        call_recs = [r for r in recs if r.trade_type == "sell_call"]
        assert len(call_recs) == 0


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


class TestRollDirection:
    """Roll recommendations should never increase risk direction."""

    def test_put_roll_never_goes_up(self) -> None:
        """A put at $270 should never roll UP to $300 — that's closer to ATM."""
        from src.intelligence.position_review import _build_roll
        from tests.fixtures.intelligence import make_intelligence_context, make_quant_intelligence
        from tests.fixtures.market_data import make_event_calendar
        from src.models.market import OptionsChain, OptionContract
        from src.models.position import Position

        # GOOG at $400, short $270 put, chain has $300 put as "best delta"
        # but $300 > $270 → should be blocked
        pos = Position(
            symbol="GOOG", position_type="short_put", quantity=1,
            strike=Decimal("270"), expiration=date(2026, 10, 16),
            entry_price=Decimal("20.00"), current_price=Decimal("8.81"),
            underlying_price=Decimal("400"), cost_basis=Decimal("27000"),
            delta=-0.04, theta=0.02, gamma=0.001, vega=0.03, iv=0.35,
            days_to_expiry=185,
        )
        # Chain only has puts at $300+ (above current $270 strike)
        chain = OptionsChain(
            symbol="GOOG",
            puts=[
                OptionContract(strike=Decimal("300"), expiration=date(2026, 5, 29),
                               option_type="put",
                               bid=Decimal("3.00"), ask=Decimal("4.00"), mid=Decimal("3.50"),
                               delta=-0.17, implied_vol=0.39, volume=50, open_interest=200),
                OptionContract(strike=Decimal("320"), expiration=date(2026, 5, 29),
                               option_type="put",
                               bid=Decimal("5.50"), ask=Decimal("6.50"), mid=Decimal("6.00"),
                               delta=-0.22, implied_vol=0.40, volume=30, open_interest=150),
            ],
            calls=[],
        )
        ctx = make_intelligence_context(
            symbol="GOOG",
            quant=make_quant_intelligence(iv_rank=74.0),
            events=make_event_calendar(symbol="GOOG", next_earnings=date(2026, 4, 29)),
        )

        result = _build_roll(pos, ctx, chain)
        # All chain puts are above $270 → should return None (no roll)
        assert result is None

    def test_put_roll_allows_same_or_lower_strike(self) -> None:
        """A put at $230 can roll DOWN to $220 (lower strike = safer)."""
        from src.intelligence.position_review import _build_roll
        from tests.fixtures.intelligence import make_intelligence_context, make_quant_intelligence
        from tests.fixtures.market_data import make_market_context
        from src.models.market import OptionsChain, OptionContract
        from src.models.position import Position

        pos = Position(
            symbol="AMZN", position_type="short_put", quantity=1,
            strike=Decimal("230"), expiration=date(2026, 5, 29),
            entry_price=Decimal("5.60"), current_price=Decimal("3.00"),
            underlying_price=Decimal("249"), cost_basis=Decimal("23000"),
            delta=-0.18, theta=0.04, gamma=0.003, vega=0.05, iv=0.42,
            days_to_expiry=45,
        )
        chain = OptionsChain(
            symbol="AMZN",
            puts=[
                OptionContract(strike=Decimal("220"), expiration=date(2026, 6, 30),
                               option_type="put",
                               bid=Decimal("4.00"), ask=Decimal("5.00"), mid=Decimal("4.50"),
                               delta=-0.15, implied_vol=0.45, volume=100, open_interest=300),
                OptionContract(strike=Decimal("210"), expiration=date(2026, 6, 30),
                               option_type="put",
                               bid=Decimal("2.50"), ask=Decimal("3.50"), mid=Decimal("3.00"),
                               delta=-0.10, implied_vol=0.44, volume=80, open_interest=250),
            ],
            calls=[],
            expirations=[date(2026, 6, 30)],
        )
        ctx = make_intelligence_context(
            symbol="AMZN",
            quant=make_quant_intelligence(iv_rank=65.0),
            market=make_market_context(symbol="AMZN", price=Decimal("249")),
        )

        result = _build_roll(pos, ctx, chain)
        # $220 and $210 are both below $230 → should find a roll
        assert result is not None
        assert result.new_strike <= pos.strike
        assert result.roll_type == "down_and_out"

    def test_call_roll_never_goes_down(self) -> None:
        """A call at $295 should never roll DOWN to $280 — that's closer to ATM."""
        from src.intelligence.position_review import _build_roll
        from tests.fixtures.intelligence import make_intelligence_context, make_quant_intelligence
        from src.models.market import OptionsChain, OptionContract
        from src.models.position import Position

        pos = Position(
            symbol="AMZN", position_type="short_call", quantity=1,
            strike=Decimal("295"), expiration=date(2026, 12, 18),
            entry_price=Decimal("15.50"), current_price=Decimal("15.62"),
            underlying_price=Decimal("249"), cost_basis=Decimal("0"),
            delta=0.15, theta=-0.03, gamma=0.002, vega=0.10, iv=0.42,
            days_to_expiry=248, option_type="call",
        )
        # Chain only has calls below $295
        chain = OptionsChain(
            symbol="AMZN",
            puts=[],
            calls=[
                OptionContract(strike=Decimal("280"), expiration=date(2026, 5, 30),
                               option_type="call",
                               bid=Decimal("2.80"), ask=Decimal("3.20"), mid=Decimal("3.01"),
                               delta=0.19, implied_vol=0.42, volume=40, open_interest=100),
                OptionContract(strike=Decimal("270"), expiration=date(2026, 5, 30),
                               option_type="call",
                               bid=Decimal("4.50"), ask=Decimal("5.00"), mid=Decimal("4.75"),
                               delta=0.25, implied_vol=0.43, volume=30, open_interest=80),
            ],
        )
        ctx = make_intelligence_context(
            symbol="AMZN",
            quant=make_quant_intelligence(iv_rank=45.0),
        )

        result = _build_roll(pos, ctx, chain)
        assert result is None

    def test_put_roll_blocked_when_debit_exceeds_premium(self) -> None:
        """A roll where the debit exceeds the new premium should be blocked."""
        from src.intelligence.position_review import _build_roll
        from tests.fixtures.intelligence import make_intelligence_context, make_quant_intelligence
        from src.models.market import OptionsChain, OptionContract
        from src.models.position import Position

        # Buy back at $5.44, new premium $2.65 → net debit $2.79 > new premium $2.65
        pos = Position(
            symbol="AMZN", position_type="short_put", quantity=1,
            strike=Decimal("230"), expiration=date(2026, 5, 29),
            entry_price=Decimal("5.60"), current_price=Decimal("5.44"),
            underlying_price=Decimal("249"), cost_basis=Decimal("23000"),
            delta=-0.22, theta=0.04, gamma=0.003, vega=0.05, iv=0.45,
            days_to_expiry=45,
        )
        chain = OptionsChain(
            symbol="AMZN",
            puts=[
                OptionContract(strike=Decimal("220"), expiration=date(2026, 5, 30),
                               option_type="put",
                               bid=Decimal("2.40"), ask=Decimal("2.90"), mid=Decimal("2.65"),
                               delta=-0.15, implied_vol=0.45, volume=100, open_interest=300),
            ],
            calls=[],
        )
        ctx = make_intelligence_context(
            symbol="AMZN",
            quant=make_quant_intelligence(iv_rank=65.0),
        )

        result = _build_roll(pos, ctx, chain)
        # Debit ($2.79) > new premium ($2.65) → blocked
        assert result is None


class TestDeepOTMEarningsAdvice:
    """Deep OTM positions with small losses shouldn't get panic advice."""

    def test_deep_otm_call_no_panic_on_small_loss(self) -> None:
        """A deep OTM call with tiny loss shouldn't say 'close to limit loss'."""
        from src.intelligence.position_review import review_position
        from tests.fixtures.intelligence import make_intelligence_context, make_quant_intelligence
        from tests.fixtures.market_data import make_event_calendar
        from src.models.position import Position

        # AMZN $295 call: stock at $249, delta ~0.05, only -1% loss
        pos = Position(
            symbol="AMZN", position_type="short_call", quantity=1,
            strike=Decimal("295"), expiration=date(2026, 12, 18),
            entry_price=Decimal("15.50"), current_price=Decimal("15.65"),
            underlying_price=Decimal("249"), cost_basis=Decimal("0"),
            delta=0.05, theta=-0.02, gamma=0.001, vega=0.08, iv=0.42,
            days_to_expiry=248, option_type="call",
        )
        ctx = make_intelligence_context(
            symbol="AMZN",
            quant=make_quant_intelligence(trend_direction="range"),
            events=make_event_calendar(symbol="AMZN", next_earnings=date(2026, 4, 30)),
        )

        result = review_position(pos, ctx)
        assert result.action == "WATCH CLOSELY"
        # Should NOT say "close before report to limit loss"
        assert "close before report to limit loss" not in result.reasoning.lower()
        # Should say something about being deep OTM / unlikely to be threatened
        assert "deep otm" in result.reasoning.lower() or "unlikely" in result.reasoning.lower()

    def test_near_atm_underwater_gets_close_advice(self) -> None:
        """A near-ATM underwater position SHOULD get 'close to limit loss' advice."""
        from src.intelligence.position_review import review_position
        from tests.fixtures.intelligence import make_intelligence_context, make_quant_intelligence
        from tests.fixtures.market_data import make_event_calendar
        from src.models.position import Position

        # Near ATM put, delta 0.35, -15% loss
        pos = Position(
            symbol="META", position_type="short_put", quantity=1,
            strike=Decimal("500"), expiration=date(2026, 12, 18),
            entry_price=Decimal("20.00"), current_price=Decimal("23.00"),
            underlying_price=Decimal("510"), cost_basis=Decimal("50000"),
            delta=-0.35, theta=0.04, gamma=0.005, vega=0.10, iv=0.45,
            days_to_expiry=248, option_type="put",
        )
        ctx = make_intelligence_context(
            symbol="META",
            quant=make_quant_intelligence(trend_direction="range"),
            events=make_event_calendar(symbol="META", next_earnings=date(2026, 4, 30)),
        )

        result = review_position(pos, ctx)
        assert result.action == "WATCH CLOSELY"
        assert "close before report to limit loss" in result.reasoning.lower()


class TestEarningsGate:
    """New recommendations should be blocked when earnings are imminent."""

    def test_put_blocked_through_earnings(self) -> None:
        """Should not recommend a new put when earnings fall before expiry."""
        from src.main import build_recommendations
        from tests.fixtures.market_data import (
            make_market_context, make_price_history,
            make_options_chain, make_event_calendar,
        )
        from src.models.signals import AlphaSignal
        from src.models.enums import SignalType

        sig = AlphaSignal(
            symbol="MSFT",
            signal_type=SignalType.SUPPORT_BOUNCE,
            strength=70.0,
            direction="sell_put",
            reasoning="test",
            expires=datetime.utcnow() + timedelta(hours=24),
        )

        mkt = make_market_context(symbol="MSFT", price=Decimal("390.00"))
        hist = make_price_history(symbol="MSFT", current_price=Decimal("390.00"))
        chain = make_options_chain(symbol="MSFT")
        # Earnings in 15 days — within the 30-day target expiration
        cal = make_event_calendar(symbol="MSFT", next_earnings=date.today() + timedelta(days=15))
        watchlist_data = [("MSFT", mkt, hist, chain, cal)]

        recs = build_recommendations([sig], watchlist_data)
        assert len(recs) == 0, "Should not recommend puts through earnings"

    def test_put_allowed_when_no_earnings(self) -> None:
        """Should recommend a put when earnings are far out."""
        from src.main import build_recommendations
        from tests.fixtures.market_data import (
            make_market_context, make_price_history,
            make_options_chain, make_event_calendar,
        )
        from src.models.signals import AlphaSignal
        from src.models.enums import SignalType

        sig = AlphaSignal(
            symbol="MSFT",
            signal_type=SignalType.SUPPORT_BOUNCE,
            strength=70.0,
            direction="sell_put",
            reasoning="test",
            expires=datetime.utcnow() + timedelta(hours=24),
        )

        mkt = make_market_context(symbol="MSFT", price=Decimal("390.00"))
        hist = make_price_history(symbol="MSFT", current_price=Decimal("390.00"))
        chain = make_options_chain(symbol="MSFT")
        # Earnings in 60 days — well past 30-day target
        cal = make_event_calendar(symbol="MSFT", next_earnings=date.today() + timedelta(days=60))
        watchlist_data = [("MSFT", mkt, hist, chain, cal)]

        recs = build_recommendations([sig], watchlist_data)
        put_recs = [r for r in recs if r.symbol == "MSFT"]
        assert len(put_recs) > 0, "Should recommend puts when earnings are far out"


class TestHighIVTakeProfit:
    """Short-dated positions in high IV should take profit earlier."""

    def test_high_iv_short_dated_takes_profit_at_50pct(self) -> None:
        """MU scenario: 64% captured, 31 DTE, IV rank 100 → TAKE PROFIT."""
        from src.intelligence.position_review import review_position
        from tests.fixtures.intelligence import make_intelligence_context, make_quant_intelligence
        from src.models.position import Position

        pos = Position(
            symbol="MU", position_type="short_put", quantity=1,
            strike=Decimal("320"), expiration=date(2026, 5, 15),
            entry_price=Decimal("7.00"), current_price=Decimal("2.53"),
            underlying_price=Decimal("420"), cost_basis=Decimal("32000"),
            delta=-0.04, theta=0.01, gamma=0.001, vega=0.02, iv=0.77,
            days_to_expiry=31,
        )
        ctx = make_intelligence_context(
            symbol="MU",
            quant=make_quant_intelligence(iv_rank=100.0, trend_direction="uptrend"),
        )

        result = review_position(pos, ctx)
        assert result.action == "TAKE PROFIT"
        assert "high iv" in result.reasoning.lower()

    def test_deep_otm_long_dated_normal_iv_holds(self) -> None:
        """Deep OTM with 185 DTE and normal IV should NOT take profit at 56%."""
        from src.intelligence.position_review import review_position
        from tests.fixtures.intelligence import make_intelligence_context, make_quant_intelligence
        from src.models.position import Position

        pos = Position(
            symbol="GOOG", position_type="short_put", quantity=1,
            strike=Decimal("270"), expiration=date(2026, 10, 16),
            entry_price=Decimal("20.00"), current_price=Decimal("8.80"),
            underlying_price=Decimal("400"), cost_basis=Decimal("27000"),
            delta=-0.04, theta=0.02, gamma=0.001, vega=0.03, iv=0.35,
            days_to_expiry=185,
        )
        ctx = make_intelligence_context(
            symbol="GOOG",
            quant=make_quant_intelligence(iv_rank=45.0, trend_direction="range"),
        )

        result = review_position(pos, ctx)
        # 56% captured but 185 DTE and moderate IV → should NOT take profit
        assert result.action != "TAKE PROFIT"


class TestLargeDollarProfit:
    """Positions with large absolute profit get a 'consider closing' note."""

    def test_large_profit_adds_watch_reason(self) -> None:
        from src.intelligence.position_review import review_position
        from src.models.position import Position
        from tests.fixtures.intelligence import make_intelligence_context, make_quant_intelligence

        # MU Aug $300 put: entry $40.80, current $15.08 → $2,572 profit (63%)
        # Deep OTM needs 80% for TAKE PROFIT, so this stays in WATCH
        # But $2,572 is a lot of money — should mention it
        pos = Position(
            symbol="MU", position_type="short_put", quantity=1,
            strike=Decimal("300"), expiration=date(2026, 8, 21),
            entry_price=Decimal("40.80"), current_price=Decimal("15.08"),
            underlying_price=Decimal("115"), cost_basis=Decimal("4080"),
            delta=-0.03, theta=0.01, gamma=0.001, vega=0.02, iv=0.45,
            days_to_expiry=129,
        )
        ctx = make_intelligence_context(
            symbol="MU",
            quant=make_quant_intelligence(iv_rank=50.0, trend_direction="range"),
        )
        result = review_position(pos, ctx)
        assert result.action == "WATCH CLOSELY"
        assert "$2,572" in result.reasoning or "2,572" in result.reasoning

    def test_small_profit_no_mention(self) -> None:
        from src.intelligence.position_review import review_position
        from src.models.position import Position
        from tests.fixtures.intelligence import make_intelligence_context, make_quant_intelligence

        # $500 profit → below $2,000 threshold, shouldn't add the note
        pos = Position(
            symbol="AAPL", position_type="short_put", quantity=1,
            strike=Decimal("200"), expiration=date(2026, 8, 21),
            entry_price=Decimal("8.00"), current_price=Decimal("3.00"),
            underlying_price=Decimal("230"), cost_basis=Decimal("800"),
            delta=-0.08, theta=0.01, gamma=0.001, vega=0.02, iv=0.30,
            days_to_expiry=129,
        )
        ctx = make_intelligence_context(
            symbol="AAPL",
            quant=make_quant_intelligence(iv_rank=40.0, trend_direction="range"),
        )
        result = review_position(pos, ctx)
        # Should not mention "consider closing" for a small profit
        assert "consider closing" not in result.reasoning


class TestHighVolEarningsMovers:
    """High-volatility names get stronger close recommendation near earnings."""

    def test_tsla_earnings_recommends_close(self) -> None:
        from src.intelligence.position_review import review_position
        from src.models.position import Position
        from tests.fixtures.intelligence import make_intelligence_context, make_quant_intelligence
        from tests.fixtures.market_data import make_event_calendar

        pos = Position(
            symbol="TSLA", position_type="short_call", quantity=2,
            strike=Decimal("425"), expiration=date(2026, 5, 15),
            entry_price=Decimal("8.50"), current_price=Decimal("3.55"),
            underlying_price=Decimal("250"), cost_basis=Decimal("1700"),
            delta=-0.02, theta=0.03, gamma=0.001, vega=0.02, iv=0.50,
            days_to_expiry=31,
        )
        ctx = make_intelligence_context(
            symbol="TSLA",
            quant=make_quant_intelligence(iv_rank=21.0, trend_direction="range"),
            events=make_event_calendar(
                next_earnings=date.today() + timedelta(days=8),
            ),
        )
        result = review_position(pos, ctx)
        assert result.action == "WATCH CLOSELY"
        assert "routinely moves" in result.reasoning or "10%+" in result.reasoning

    def test_non_volatile_name_says_safe_to_hold(self) -> None:
        from src.intelligence.position_review import review_position
        from src.models.position import Position
        from tests.fixtures.intelligence import make_intelligence_context, make_quant_intelligence
        from tests.fixtures.market_data import make_event_calendar

        # GOOG is NOT in the high-vol list, should say "safe to hold"
        pos = Position(
            symbol="GOOG", position_type="short_put", quantity=1,
            strike=Decimal("270"), expiration=date(2026, 10, 16),
            entry_price=Decimal("20.00"), current_price=Decimal("8.80"),
            underlying_price=Decimal("400"), cost_basis=Decimal("2000"),
            delta=-0.04, theta=0.02, gamma=0.001, vega=0.03, iv=0.35,
            days_to_expiry=185,
        )
        ctx = make_intelligence_context(
            symbol="GOOG",
            quant=make_quant_intelligence(iv_rank=45.0, trend_direction="range"),
            events=make_event_calendar(
                next_earnings=date.today() + timedelta(days=15),
            ),
        )
        result = review_position(pos, ctx)
        assert result.action == "WATCH CLOSELY"
        assert "safe to hold" in result.reasoning


class TestOpportunitiesSection:
    """OPPORTUNITIES section shows proactive deployment recommendations."""

    def test_opportunities_appear_when_conditions_met(self) -> None:
        """Stock near support with decent IV and positive TV → shows opportunity."""
        from src.main import format_local_briefing
        from src.monitor.regime import RegimeState
        from src.models.position import PortfolioState
        from tests.fixtures.market_data import (
            make_market_context, make_price_history, make_options_chain, make_event_calendar,
        )
        from tests.fixtures.intelligence import make_intelligence_context, make_quant_intelligence

        regime = RegimeState(
            regime="hold", vix=20.0, spy_change_pct=0.005,
            severity="normal", target_deployed=0.70,
            timestamp=datetime.utcnow(),
        )
        mkt = make_market_context(
            symbol="AAPL", iv_rank=55.0, price=Decimal("195"),
            price_change_1d=-0.02,
        )
        hist = make_price_history(
            symbol="AAPL", current_price=Decimal("195"),
            sma_200=Decimal("192"), sma_50=Decimal("198"),
            rsi_14=38.0,
        )
        from src.models.market import OptionContract
        exp_date = date.today() + timedelta(days=37)
        chain = make_options_chain(
            symbol="AAPL",
            puts=[
                OptionContract(
                    strike=Decimal("185"), expiration=exp_date,
                    option_type="put", bid=Decimal("5.00"), ask=Decimal("5.40"),
                    mid=Decimal("5.20"), volume=500, open_interest=2000,
                    implied_vol=0.42, delta=-0.22,
                ),
                OptionContract(
                    strike=Decimal("180"), expiration=exp_date,
                    option_type="put", bid=Decimal("3.80"), ask=Decimal("4.20"),
                    mid=Decimal("4.00"), volume=300, open_interest=1500,
                    implied_vol=0.40, delta=-0.16,
                ),
            ],
        )
        cal = make_event_calendar(
            symbol="AAPL",
            next_earnings=date.today() + timedelta(days=60),  # far out
        )
        watchlist_data = [(
            "AAPL", mkt, hist, chain, cal,
        )]
        intel_ctx = make_intelligence_context(
            symbol="AAPL",
            quant=make_quant_intelligence(iv_rank=55.0),
        )
        # Manually set TV consensus
        from src.models.intelligence import TechnicalConsensus
        intel_ctx = make_intelligence_context(
            symbol="AAPL",
            quant=make_quant_intelligence(iv_rank=55.0),
            technical_consensus=TechnicalConsensus(
                source="tradingview", overall="BUY", oscillators="BUY",
                moving_averages="BUY", buy_count=15, neutral_count=5, sell_count=6,
            ),
        )
        portfolio = PortfolioState(
            cash_available=Decimal("150000"),
            net_liquidation=Decimal("1000000"),
        )
        briefing = format_local_briefing(
            regime=regime, vix=20.0, spy_change=0.005,
            all_signals=[], watchlist_data=watchlist_data, tax_alerts=[],
            intel_contexts=[intel_ctx],
            portfolio_state=portfolio,
        )
        import re
        clean = re.sub(r'\033\[[0-9;]*m', '', briefing)
        assert "OPPORTUNITIES" in clean
        assert "AAPL" in clean
        assert "$150,000 cash available" in clean
        # Option contract details should appear for SELL PUT
        assert "Strike: $185" in clean
        assert "37d" in clean
        assert "Bid: $5.00" in clean
        assert "Delta: 0.22" in clean
        assert "Premium: $5.20/contract" in clean

    def test_no_opportunities_when_no_cash(self) -> None:
        """No cash = no OPPORTUNITIES section."""
        from src.main import format_local_briefing
        from src.monitor.regime import RegimeState
        from src.models.position import PortfolioState

        regime = RegimeState(
            regime="hold", vix=20.0, spy_change_pct=0.005,
            severity="normal", target_deployed=0.70,
            timestamp=datetime.utcnow(),
        )
        portfolio = PortfolioState(
            cash_available=Decimal("0"),
            net_liquidation=Decimal("1000000"),
        )
        briefing = format_local_briefing(
            regime=regime, vix=20.0, spy_change=0.005,
            all_signals=[], watchlist_data=[], tax_alerts=[],
            portfolio_state=portfolio,
        )
        import re
        clean = re.sub(r'\033\[[0-9;]*m', '', briefing)
        assert "OPPORTUNITIES" not in clean

    def test_cash_status_in_header(self) -> None:
        """Portfolio NLV and cash appear in the header."""
        from src.main import format_local_briefing
        from src.monitor.regime import RegimeState
        from src.models.position import PortfolioState

        regime = RegimeState(
            regime="hold", vix=20.0, spy_change_pct=0.005,
            severity="normal", target_deployed=0.70,
            timestamp=datetime.utcnow(),
        )
        portfolio = PortfolioState(
            cash_available=Decimal("120000"),
            net_liquidation=Decimal("980000"),
        )
        briefing = format_local_briefing(
            regime=regime, vix=20.0, spy_change=0.005,
            all_signals=[], watchlist_data=[], tax_alerts=[],
            portfolio_state=portfolio,
        )
        import re
        clean = re.sub(r'\033\[[0-9;]*m', '', briefing)
        assert "$980,000" in clean  # NLV
        assert "$120,000" in clean  # cash
        assert "Deployed:" in clean


class TestWheelFocusedRecommendations:
    """Recommendations should be wheel-focused: sell puts or buy 100 shares."""

    def test_sell_put_default_for_expensive_stocks(self) -> None:
        """Stocks too expensive for 100 shares should recommend SELL PUT, not BUY SHARES."""
        from src.main import format_local_briefing
        from src.monitor.regime import RegimeState
        from src.models.position import PortfolioState
        from src.models.market import OptionContract
        from tests.fixtures.market_data import (
            make_market_context, make_price_history, make_options_chain, make_event_calendar,
        )
        from tests.fixtures.intelligence import make_intelligence_context, make_quant_intelligence
        from src.models.intelligence import TechnicalConsensus

        regime = RegimeState(
            regime="hold", vix=20.0, spy_change_pct=0.005,
            severity="normal", target_deployed=0.70,
            timestamp=datetime.utcnow(),
        )
        # Expensive stock: 100 shares = $50K > 5% of $1M NLV
        mkt = make_market_context(symbol="NVDA", iv_rank=35.0, price=Decimal("500"),
                                  price_change_1d=-0.01)
        hist = make_price_history(symbol="NVDA", current_price=Decimal("500"),
                                  sma_200=Decimal("490"), sma_50=Decimal("510"), rsi_14=42.0)
        exp_date = date.today() + timedelta(days=37)
        chain = make_options_chain(symbol="NVDA", puts=[
            OptionContract(strike=Decimal("480"), expiration=exp_date, option_type="put",
                           bid=Decimal("13.00"), ask=Decimal("13.40"), mid=Decimal("13.20"),
                           volume=1000, open_interest=5000, implied_vol=0.45, delta=-0.22),
        ])
        cal = make_event_calendar(symbol="NVDA", next_earnings=date.today() + timedelta(days=60))
        intel_ctx = make_intelligence_context(
            symbol="NVDA", quant=make_quant_intelligence(iv_rank=35.0),
            technical_consensus=TechnicalConsensus(
                source="tradingview", overall="BUY", oscillators="BUY",
                moving_averages="BUY", buy_count=16, neutral_count=4, sell_count=6,
            ),
        )
        portfolio = PortfolioState(
            cash_available=Decimal("150000"), net_liquidation=Decimal("1000000"),
        )
        briefing = format_local_briefing(
            regime=regime, vix=20.0, spy_change=0.005,
            all_signals=[], watchlist_data=[("NVDA", mkt, hist, chain, cal)],
            tax_alerts=[], intel_contexts=[intel_ctx], portfolio_state=portfolio,
        )
        import re
        clean = re.sub(r'\033\[[0-9;]*m', '', briefing)
        # Should be SELL PUT, NOT BUY SHARES
        assert "SELL PUT" in clean
        assert "BUY 100 SHARES" not in clean
        assert "BUY SHARES" not in clean

    def test_reallocation_shows_underperformers(self) -> None:
        """Positions down significantly should appear in REALLOCATE section."""
        from src.main import format_local_briefing
        from src.monitor.regime import RegimeState
        from src.models.position import PortfolioState, Position

        regime = RegimeState(
            regime="hold", vix=20.0, spy_change_pct=0.005,
            severity="normal", target_deployed=0.70,
            timestamp=datetime.utcnow(),
        )
        loser = Position(
            symbol="INTC", position_type="long_stock", quantity=200,
            strike=Decimal("0"), expiration=None,
            entry_price=Decimal("40"), current_price=Decimal("0"),
            underlying_price=Decimal("30"), cost_basis=Decimal("40"),
            delta=1.0, theta=0.0, gamma=0.0, vega=0.0, iv=0.0,
        )
        portfolio = PortfolioState(
            positions=[loser],
            cash_available=Decimal("50000"),
            net_liquidation=Decimal("1000000"),
        )
        briefing = format_local_briefing(
            regime=regime, vix=20.0, spy_change=0.005,
            all_signals=[], watchlist_data=[], tax_alerts=[],
            portfolio_state=portfolio,
        )
        import re
        clean = re.sub(r'\033\[[0-9;]*m', '', briefing)
        assert "REALLOCATE" in clean
        assert "INTC" in clean
        assert "down" in clean.lower()
        assert "redeploy" in clean.lower()

    def test_small_position_flagged_for_reallocation(self) -> None:
        """< 100 shares that can't sell covered calls should be flagged."""
        from src.main import format_local_briefing
        from src.monitor.regime import RegimeState
        from src.models.position import PortfolioState, Position

        regime = RegimeState(
            regime="hold", vix=20.0, spy_change_pct=0.005,
            severity="normal", target_deployed=0.70,
            timestamp=datetime.utcnow(),
        )
        small_pos = Position(
            symbol="COIN", position_type="long_stock", quantity=25,
            strike=Decimal("0"), expiration=None,
            entry_price=Decimal("200"), current_price=Decimal("0"),
            underlying_price=Decimal("180"), cost_basis=Decimal("200"),
            delta=1.0, theta=0.0, gamma=0.0, vega=0.0, iv=0.0,
        )
        portfolio = PortfolioState(
            positions=[small_pos],
            cash_available=Decimal("50000"),
            net_liquidation=Decimal("1000000"),
        )
        briefing = format_local_briefing(
            regime=regime, vix=20.0, spy_change=0.005,
            all_signals=[], watchlist_data=[], tax_alerts=[],
            portfolio_state=portfolio,
        )
        import re
        clean = re.sub(r'\033\[[0-9;]*m', '', briefing)
        assert "REALLOCATE" in clean
        assert "COIN" in clean
        assert "covered calls" in clean.lower()


class TestYTDOrderParsing:
    """Tests for E*Trade order parsing → TaxEngine population."""

    def _make_order(self, action: str, symbol: str, strike: str,
                    expiry: str, callput: str, qty: int, price: str,
                    exec_time: Any = None, acct: str = "acct1") -> dict:
        """Helper to build an E*Trade order dict."""
        return {
            "OrderDetail": [{
                "executedTime": exec_time,
                "Instrument": [{
                    "Product": {
                        "securityType": "OPTN",
                        "symbol": symbol,
                        "strikePrice": strike,
                        "expiryYear": expiry.split("-")[0],
                        "expiryMonth": expiry.split("-")[1],
                        "expiryDay": expiry.split("-")[2],
                        "callPut": callput,
                    },
                    "orderAction": action,
                    "filledQuantity": str(qty),
                    "averageExecutionPrice": price,
                }],
            }],
            "_account_id": acct,
        }

    def test_sell_open_adds_premium_income(self) -> None:
        from src.data.broker import populate_tax_engine_from_orders

        orders = [self._make_order(
            "SELL_OPEN", "AAPL", "220", "2026-04-18", "PUT", 1, "1.50",
        )]
        engine = populate_tax_engine_from_orders(orders)
        assert engine.option_premium_income_ytd == Decimal("150")

    def test_buy_close_records_profit(self) -> None:
        from src.data.broker import populate_tax_engine_from_orders

        orders = [
            self._make_order("SELL_OPEN", "MSFT", "400", "2026-05-16", "PUT", 1, "3.00"),
            self._make_order("BUY_CLOSE", "MSFT", "400", "2026-05-16", "PUT", 1, "0.80"),
        ]
        engine = populate_tax_engine_from_orders(orders)
        # Sold for $3.00, bought back for $0.80 → $220 profit per contract
        assert engine.realized_stcg_ytd == Decimal("220")
        assert engine.option_premium_income_ytd == Decimal("300")

    def test_buy_close_at_loss_records_loss(self) -> None:
        from src.data.broker import populate_tax_engine_from_orders

        orders = [
            self._make_order("SELL_OPEN", "TSLA", "200", "2026-05-16", "PUT", 1, "2.00"),
            self._make_order("BUY_CLOSE", "TSLA", "200", "2026-05-16", "PUT", 1, "5.00",
                             exec_time=1713052800000),
        ]
        engine = populate_tax_engine_from_orders(orders)
        # Sold for $200, bought back for $500 → $300 loss
        assert engine.realized_losses_ytd == Decimal("300")
        assert engine.option_premium_income_ytd == Decimal("200")

    def test_non_option_orders_ignored(self) -> None:
        from src.data.broker import populate_tax_engine_from_orders

        orders = [{
            "OrderDetail": [{
                "executedTime": None,
                "Instrument": [{
                    "Product": {"securityType": "EQ", "symbol": "AAPL"},
                    "orderAction": "BUY",
                    "filledQuantity": "10",
                    "averageExecutionPrice": "175.00",
                }],
            }],
            "_account_id": "acct1",
        }]
        engine = populate_tax_engine_from_orders(orders)
        assert engine.option_premium_income_ytd == Decimal("0")
        assert engine.realized_stcg_ytd == Decimal("0")

    def test_loss_close_records_wash_sale(self) -> None:
        from src.data.broker import populate_tax_engine_from_orders
        import time as _time

        recent_epoch_ms = int((_time.time() - 5 * 86400) * 1000)

        orders = [
            self._make_order("SELL_OPEN", "AMZN", "230", "2026-05-16", "PUT", 1, "1.00"),
            self._make_order("BUY_CLOSE", "AMZN", "230", "2026-05-16", "PUT", 1, "3.00",
                             exec_time=recent_epoch_ms),
        ]
        engine = populate_tax_engine_from_orders(orders)
        assert engine.realized_losses_ytd == Decimal("200")
        blocked = engine.wash_sale_tracker.get_blocked_tickers()
        assert "AMZN" in blocked

    def test_multi_contract_sell_open(self) -> None:
        from src.data.broker import populate_tax_engine_from_orders

        orders = [self._make_order(
            "SELL_OPEN", "NVDA", "100", "2026-04-11", "PUT", 2, "1.25",
        )]
        engine = populate_tax_engine_from_orders(orders)
        # 2 contracts × $1.25 × 100 = $250
        assert engine.option_premium_income_ytd == Decimal("250")

    def test_buy_close_without_matching_open_skipped(self) -> None:
        """BUY_CLOSE with no matching SELL_OPEN (opened last year) is skipped."""
        from src.data.broker import populate_tax_engine_from_orders

        orders = [
            self._make_order("BUY_CLOSE", "GOOG", "180", "2026-03-21", "PUT", 1, "0.50"),
        ]
        engine = populate_tax_engine_from_orders(orders)
        assert engine.realized_stcg_ytd == Decimal("0")
        assert engine.realized_losses_ytd == Decimal("0")

    def test_briefing_shows_ytd_pnl_in_header(self) -> None:
        """YTD P&L line appears in the briefing header when tax_engine provided."""
        from src.models.tax import TaxEngine

        tax_engine = TaxEngine(
            option_premium_income_ytd=Decimal("5000"),
            realized_stcg_ytd=Decimal("3500"),
            realized_losses_ytd=Decimal("800"),
        )
        from src.main import format_local_briefing
        from src.monitor.regime import RegimeState

        regime = RegimeState(
            regime="hold", vix=20.0, spy_change_pct=0.005,
            severity="normal", target_deployed=0.70,
            timestamp=datetime.utcnow(),
        )
        briefing = format_local_briefing(
            regime=regime, vix=20.0, spy_change=0.005,
            all_signals=[], watchlist_data=[], tax_alerts=[],
            tax_engine=tax_engine,
        )
        # Strip ANSI for easy checking
        import re
        clean = re.sub(r'\033\[[0-9;]*m', '', briefing)
        assert "YTD OPTIONS P&L" in clean or "YTD P&L" in clean
        assert "$+2,700" in clean  # 3500 - 800 = 2700
        assert "$5,000" in clean  # premium

    def test_briefing_shows_ytd_detail_section(self) -> None:
        """Full YTD OPTIONS P&L section appears with breakdown."""
        from src.models.tax import TaxEngine

        tax_engine = TaxEngine(
            option_premium_income_ytd=Decimal("12000"),
            realized_stcg_ytd=Decimal("8000"),
            realized_losses_ytd=Decimal("2000"),
        )
        from src.main import format_local_briefing
        from src.monitor.regime import RegimeState

        regime = RegimeState(
            regime="hold", vix=20.0, spy_change_pct=0.005,
            severity="normal", target_deployed=0.70,
            timestamp=datetime.utcnow(),
        )
        briefing = format_local_briefing(
            regime=regime, vix=20.0, spy_change=0.005,
            all_signals=[], watchlist_data=[], tax_alerts=[],
            tax_engine=tax_engine,
        )
        import re
        clean = re.sub(r'\033\[[0-9;]*m', '', briefing)
        assert "YTD OPTIONS P&L" in clean
        assert "Premium collected" in clean
        assert "$12,000" in clean
        assert "Realized gains" in clean
        assert "Net realized" in clean
