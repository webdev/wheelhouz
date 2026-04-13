"""Tests for src/analysis/ — signals, strikes, sizing, scanner, opportunities."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from src.models.enums import PositionAction, SignalType
from src.models.market import EventCalendar, MarketContext, OptionsChain, PriceHistory
from src.models.position import PortfolioState, Position
from src.models.signals import AlphaSignal

from src.analysis.signals import (
    detect_all_signals,
    detect_intraday_dip,
    detect_iv_rank_spike,
    detect_macro_fear,
    detect_multi_day_pullback,
    detect_oversold_rsi,
    detect_support_bounce,
    detect_term_inversion,
    detect_volume_climax,
)
from src.analysis.strikes import find_smart_strikes
from src.analysis.sizing import size_position
from src.analysis.scanner import scan_position
from src.analysis.opportunities import find_and_rank_opportunities, _composite_score

from tests.fixtures.market_data import (
    make_market_context,
    make_price_history,
    make_event_calendar,
    make_options_chain,
)
from tests.fixtures.sample_portfolio import make_portfolio_state, make_position
from tests.fixtures.trades import make_alpha_signal, make_smart_strike


# ── Signal Tests ─────────────────────────────────────────────────


class TestIntraDayDip:
    def test_fires_on_big_drop(self) -> None:
        mkt = make_market_context(price_change_1d=-3.5, iv_rank=65)
        sig = detect_intraday_dip("NVDA", mkt)
        assert sig is not None
        assert sig.signal_type == SignalType.INTRADAY_DIP
        assert sig.direction == "sell_put"
        assert sig.strength > 0

    def test_does_not_fire_on_green_day(self) -> None:
        mkt = make_market_context(price_change_1d=1.5)
        assert detect_intraday_dip("NVDA", mkt) is None

    def test_does_not_fire_on_small_drop(self) -> None:
        mkt = make_market_context(price_change_1d=-1.0)
        assert detect_intraday_dip("NVDA", mkt) is None

    def test_strength_boosted_by_high_iv(self) -> None:
        low_iv = make_market_context(price_change_1d=-3.0, iv_rank=30)
        high_iv = make_market_context(price_change_1d=-3.0, iv_rank=70)
        sig_low = detect_intraday_dip("NVDA", low_iv)
        sig_high = detect_intraday_dip("NVDA", high_iv)
        assert sig_low is not None and sig_high is not None
        assert sig_high.strength > sig_low.strength


class TestMultiDayPullback:
    def test_fires_on_pullback(self) -> None:
        # 5 consecutive red days
        closes = [Decimal(str(x)) for x in [900, 895, 885, 875, 860, 850]]
        hist = make_price_history(daily_closes=closes, current_price=Decimal("850"))
        sig = detect_multi_day_pullback("NVDA", hist)
        assert sig is not None
        assert sig.signal_type == SignalType.MULTI_DAY_PULLBACK

    def test_does_not_fire_on_flat(self) -> None:
        closes = [Decimal("875")] * 10
        hist = make_price_history(daily_closes=closes, current_price=Decimal("875"))
        assert detect_multi_day_pullback("NVDA", hist) is None


class TestIVRankSpike:
    def test_fires_on_spike(self) -> None:
        mkt = make_market_context(iv_rank=75, iv_rank_change_5d=25)
        sig = detect_iv_rank_spike("NVDA", mkt)
        assert sig is not None
        assert sig.signal_type == SignalType.IV_RANK_SPIKE

    def test_does_not_fire_on_low_iv(self) -> None:
        mkt = make_market_context(iv_rank=40, iv_rank_change_5d=5)
        assert detect_iv_rank_spike("NVDA", mkt) is None


class TestSupportBounce:
    def test_fires_near_200sma(self) -> None:
        mkt = make_market_context(price=Decimal("825"))
        hist = make_price_history(
            current_price=Decimal("825"),
            sma_200=Decimal("820"),
        )
        sig = detect_support_bounce("NVDA", mkt, hist)
        assert sig is not None
        assert "200 SMA" in sig.reasoning

    def test_does_not_fire_far_from_support(self) -> None:
        mkt = make_market_context(price=Decimal("900"))
        hist = make_price_history(current_price=Decimal("900"), sma_200=Decimal("750"))
        assert detect_support_bounce("NVDA", mkt, hist) is None


class TestOversoldRSI:
    def test_fires_on_low_rsi(self) -> None:
        hist = make_price_history(rsi_14=22.0)
        sig = detect_oversold_rsi("NVDA", hist)
        assert sig is not None
        assert sig.signal_type == SignalType.OVERSOLD_RSI

    def test_does_not_fire_on_normal_rsi(self) -> None:
        hist = make_price_history(rsi_14=55.0)
        assert detect_oversold_rsi("NVDA", hist) is None


class TestMacroFear:
    def test_fires_on_vix_spike(self) -> None:
        mkt = make_market_context(vix=30.0, vix_change_1d=4.0)
        sig = detect_macro_fear("NVDA", mkt)
        assert sig is not None
        assert sig.signal_type == SignalType.MACRO_FEAR_SPIKE

    def test_does_not_fire_on_calm_vix(self) -> None:
        mkt = make_market_context(vix=15.0, vix_change_1d=0.5)
        assert detect_macro_fear("NVDA", mkt) is None


class TestTermInversion:
    def test_fires_on_inversion(self) -> None:
        chain = make_options_chain(iv_by_expiry={"front_month": 0.55, "second_month": 0.44})
        sig = detect_term_inversion("NVDA", chain)
        assert sig is not None
        assert sig.signal_type == SignalType.TERM_STRUCTURE_INVERSION

    def test_does_not_fire_in_contango(self) -> None:
        chain = make_options_chain(iv_by_expiry={"front_month": 0.40, "second_month": 0.44})
        assert detect_term_inversion("NVDA", chain) is None


class TestVolumeClimax:
    def test_fires_on_huge_volume_down_day(self) -> None:
        volumes = [50_000_000.0] * 20 + [200_000_000.0]  # 4x spike
        mkt = make_market_context(price_change_1d=-2.0)
        hist = make_price_history(daily_volumes=volumes)
        sig = detect_volume_climax("NVDA", mkt, hist)
        assert sig is not None
        assert sig.signal_type == SignalType.VOLUME_CLIMAX

    def test_does_not_fire_on_up_day(self) -> None:
        volumes = [50_000_000.0] * 21
        mkt = make_market_context(price_change_1d=2.0)
        hist = make_price_history(daily_volumes=volumes)
        assert detect_volume_climax("NVDA", mkt, hist) is None


class TestDetectAllSignals:
    def test_aggregates_multiple_signals(self) -> None:
        mkt = make_market_context(
            price_change_1d=-4.0,
            iv_rank=75,
            iv_rank_change_5d=25,
            vix=30.0,
            vix_change_1d=4.0,
        )
        hist = make_price_history(rsi_14=25.0)
        chain = make_options_chain()
        cal = make_event_calendar()

        signals = detect_all_signals("NVDA", mkt, hist, chain, cal)
        assert len(signals) >= 2  # at least dip + oversold RSI + macro fear
        types = {s.signal_type for s in signals}
        assert SignalType.INTRADAY_DIP in types
        assert SignalType.OVERSOLD_RSI in types


# ── Strike Tests ─────────────────────────────────────────────────


class TestSmartStrikes:
    def test_returns_candidates(self) -> None:
        hist = make_price_history()
        chain = make_options_chain()
        strikes = find_smart_strikes("NVDA", chain, hist, "sell_put")
        assert len(strikes) > 0
        assert all(s.strike > 0 for s in strikes)

    def test_sorted_by_score(self) -> None:
        hist = make_price_history()
        chain = make_options_chain()
        strikes = find_smart_strikes("NVDA", chain, hist, "sell_put")
        scores = [s.strike_score for s in strikes]
        assert scores == sorted(scores, reverse=True)


# ── Sizing Tests ─────────────────────────────────────────────────


class TestSizing:
    def test_high_conviction_larger_size(self) -> None:
        strike = make_smart_strike()
        portfolio = make_portfolio_state()
        high_signals = [
            make_alpha_signal(strength=80),
            make_alpha_signal(strength=75, signal_type=SignalType.IV_RANK_SPIKE),
            make_alpha_signal(strength=72, signal_type=SignalType.SUPPORT_BOUNCE),
        ]
        low_signals = [make_alpha_signal(strength=30)]

        high = size_position("NVDA", "sell_put", strike, date.today() + timedelta(days=30),
                             high_signals, portfolio)
        low = size_position("NVDA", "sell_put", strike, date.today() + timedelta(days=30),
                            low_signals, portfolio)

        assert high.conviction == "high"
        assert low.conviction == "low"
        assert high.contracts >= low.contracts

    def test_respects_concentration_limit(self) -> None:
        # Use a low-priced strike so 1 contract isn't 8% of NLV
        strike = make_smart_strike(strike=Decimal("50.00"), premium=Decimal("1.50"))
        portfolio = make_portfolio_state(
            concentration={"CRM": 0.09},  # already at 9% of 10% limit
        )
        signals = [make_alpha_signal(symbol="CRM", strength=80),
                    make_alpha_signal(symbol="CRM", strength=75)]

        sized = size_position("CRM", "sell_put", strike,
                              date.today() + timedelta(days=30), signals, portfolio)
        # At $50 strike, 1 contract = $5K = 0.5% of $1M. Should be capped at ~1%.
        assert sized.contracts <= 2
        assert sized.portfolio_pct <= 0.02

    def test_uses_decimal_for_money(self) -> None:
        strike = make_smart_strike()
        portfolio = make_portfolio_state()
        signals = [make_alpha_signal()]
        sized = size_position("NVDA", "sell_put", strike,
                              date.today() + timedelta(days=30), signals, portfolio)
        assert isinstance(sized.capital_deployed, Decimal)
        assert isinstance(sized.strike, Decimal)
        assert isinstance(sized.premium, Decimal)


# ── Scanner Tests ────────────────────────────────────────────────


class TestScanner:
    def test_close_early_at_profit_target(self) -> None:
        pos = make_position(profit_pct=0.55, days_to_expiry=25)
        mkt = make_market_context()
        cal = make_event_calendar()
        action, reason = scan_position(pos, mkt, cal, [])
        assert action == PositionAction.CLOSE_EARLY

    def test_let_expire_far_otm_near_expiry(self) -> None:
        pos = make_position(days_to_expiry=3, delta=-0.05)
        mkt = make_market_context()
        cal = make_event_calendar()
        action, _ = scan_position(pos, mkt, cal, [])
        assert action == PositionAction.LET_EXPIRE

    def test_close_and_reload_with_signal(self) -> None:
        pos = make_position(profit_pct=0.45)
        mkt = make_market_context()
        cal = make_event_calendar()
        other_signal = make_alpha_signal(symbol="AMD", strength=75)
        action, reason = scan_position(pos, mkt, cal, [other_signal])
        assert action == PositionAction.CLOSE_AND_RELOAD
        assert "AMD" in reason

    def test_alert_earnings_conflict(self) -> None:
        pos = make_position(
            days_to_expiry=30,
            expiration=date.today() + timedelta(days=30),
            profit_pct=0.10,
            delta=-0.30,
        )
        mkt = make_market_context()
        cal = make_event_calendar(next_earnings=date.today() + timedelta(days=15))
        action, _ = scan_position(pos, mkt, cal, [])
        assert action == PositionAction.ALERT_EARNINGS

    def test_monitor_healthy_position(self) -> None:
        pos = make_position(
            profit_pct=0.20,
            days_to_expiry=25,
            delta=-0.20,
            distance_from_strike_pct=8.0,
        )
        mkt = make_market_context()
        cal = make_event_calendar(next_earnings=date.today() + timedelta(days=60))
        action, _ = scan_position(pos, mkt, cal, [])
        assert action == PositionAction.MONITOR

    def test_loss_stop_fires(self) -> None:
        # Current price 2.5x entry (loss stop is 2x for monthlies)
        pos = make_position(
            entry_price=Decimal("5.00"),
            current_price=Decimal("12.50"),
            profit_pct=-1.5,
            days_to_expiry=25,
        )
        mkt = make_market_context()
        cal = make_event_calendar()
        action, reason = scan_position(pos, mkt, cal, [])
        assert action == PositionAction.CLOSE_EARLY
        assert "Loss stop" in reason


# ── Opportunity Pipeline Tests ───────────────────────────────────


class TestOpportunityPipeline:
    def test_composite_score(self) -> None:
        from tests.fixtures.trades import make_sized_opportunity
        high = make_sized_opportunity(conviction="high", annualized_yield=0.25)
        low = make_sized_opportunity(conviction="low", annualized_yield=0.25)
        assert _composite_score(high) > _composite_score(low)

    def test_pipeline_skips_no_data(self) -> None:
        portfolio = make_portfolio_state()
        result = find_and_rank_opportunities(
            watchlist=["FAKE"],
            market_data={},
            price_histories={},
            option_chains={},
            event_calendars={},
            portfolio=portfolio,
        )
        assert result == []

    def test_pipeline_produces_ranked_output(self) -> None:
        mkt = make_market_context(price_change_1d=-3.5, iv_rank=70)
        hist = make_price_history(rsi_14=28.0)
        chain = make_options_chain()
        cal = make_event_calendar(next_earnings=date.today() + timedelta(days=60))
        portfolio = make_portfolio_state()

        result = find_and_rank_opportunities(
            watchlist=["NVDA"],
            market_data={"NVDA": mkt},
            price_histories={"NVDA": hist},
            option_chains={"NVDA": chain},
            event_calendars={"NVDA": cal},
            portfolio=portfolio,
        )
        assert len(result) >= 1
        assert result[0].symbol == "NVDA"
        assert result[0].conviction in ("high", "medium", "low")
        assert isinstance(result[0].capital_deployed, Decimal)
