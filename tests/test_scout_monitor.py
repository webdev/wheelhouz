"""Tests for Scout and Monitor modules."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from src.monitor.continuous import (
    MonitorState,
    TripwireConfig,
    TripwireEvent,
    check_iv_tripwires,
    check_position_tripwires,
    check_price_tripwires,
)
from src.monitor.regime import (
    RegimeState,
    RegimeThresholds,
    classify_regime,
    detect_regime_change,
    format_regime_alert,
)
from src.monitor.sentinel import (
    SentinelAlert,
    check_premarket,
    format_sentinel_alert,
)
from src.scout.aggregator import (
    RawMention,
    ScoutAnalysis,
    ScoutOpportunity,
    aggregate_mentions,
    calculate_buzz_score,
    calculate_composite_score,
    format_scout_picks,
)
from src.scout.alerts import (
    ScoutAlertState,
    filter_for_alert,
    format_scout_alert,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mentions() -> list[RawMention]:
    now = datetime.utcnow()
    return [
        RawMention("AAPL", "unusual_whales", "twitter", now, "AAPL puts active", "@uw", 50000, 200),
        RawMention("AAPL", "r/thetagang", "reddit", now - timedelta(hours=1), "AAPL dip", "user1", 0, 50),
        RawMention("AAPL", "benzinga", "news_api", now - timedelta(hours=2), "AAPL down 3%", "bz", 0, 100),
        RawMention("NVDA", "unusual_whales", "twitter", now, "NVDA flow", "@uw", 50000, 600),
        RawMention("MSFT", "r/options", "reddit", now - timedelta(hours=8), "MSFT puts", "user2", 0, 10),
    ]


@pytest.fixture
def scout_analysis() -> ScoutAnalysis:
    return ScoutAnalysis(
        ticker="AAPL",
        buzz_score=75,
        sentiment="bullish",
        catalyst="Unusual put selling activity",
        catalyst_type="flow",
        credibility_score=70.0,
        novelty="new",
        wheel_fit="excellent",
        wheel_fit_reasoning="High IV rank, liquid options",
        recommended_strategy="monthly_put",
        urgency="now",
    )


# ---------------------------------------------------------------------------
# Scout Aggregator
# ---------------------------------------------------------------------------

class TestScoutAggregator:
    def test_aggregate_deduplicates(self, mentions: list[RawMention]) -> None:
        result = aggregate_mentions(mentions)
        assert "AAPL" in result
        assert len(result["AAPL"]) == 3

    def test_aggregate_filters_old(self, mentions: list[RawMention]) -> None:
        # MSFT mention is 8 hours old, beyond 6hr lookback
        result = aggregate_mentions(mentions)
        assert "MSFT" not in result

    def test_aggregate_high_engagement_single(self) -> None:
        now = datetime.utcnow()
        mentions = [
            RawMention("TSLA", "unusual_whales", "twitter", now, "TSLA flow", "@uw", 0, 800),
            RawMention("TSLA", "unusual_whales", "twitter", now - timedelta(minutes=30), "more TSLA", "@uw", 0, 300),
        ]
        # Only 1 source but engagement >= 500 on first mention
        result = aggregate_mentions(mentions)
        assert "TSLA" in result

    def test_aggregate_below_threshold(self) -> None:
        now = datetime.utcnow()
        mentions = [
            RawMention("XYZ", "r/wsb", "reddit", now, "XYZ moon", "user", 0, 5),
        ]
        result = aggregate_mentions(mentions)
        assert "XYZ" not in result

    def test_buzz_score(self, mentions: list[RawMention]) -> None:
        aapl_mentions = [m for m in mentions if m.ticker == "AAPL"]
        score = calculate_buzz_score(aapl_mentions)
        assert 0 < score <= 100

    def test_composite_score(self, scout_analysis: ScoutAnalysis) -> None:
        score = calculate_composite_score(scout_analysis, annualized_yield=0.12)
        assert score > 0

    def test_format_picks(self, scout_analysis: ScoutAnalysis) -> None:
        opps = [ScoutOpportunity(
            ticker="AAPL", analysis=scout_analysis,
            composite_score=80, is_qualified=True,
        )]
        output = format_scout_picks(opps)
        assert "AAPL" in output
        assert "SCOUT PICKS" in output


# ---------------------------------------------------------------------------
# Scout Alerts
# ---------------------------------------------------------------------------

class TestScoutAlerts:
    def test_filter_qualified(self, scout_analysis: ScoutAnalysis) -> None:
        opps = [ScoutOpportunity(
            ticker="AAPL", analysis=scout_analysis,
            composite_score=80, is_qualified=True,
        )]
        state = ScoutAlertState()
        result = filter_for_alert(opps, state)
        assert len(result) == 1
        assert state.alerts_today == 1

    def test_filter_not_urgent(self, scout_analysis: ScoutAnalysis) -> None:
        scout_analysis.urgency = "this_week"
        opps = [ScoutOpportunity(
            ticker="AAPL", analysis=scout_analysis,
            composite_score=80, is_qualified=True,
        )]
        state = ScoutAlertState()
        result = filter_for_alert(opps, state)
        assert len(result) == 0

    def test_filter_daily_cap(self, scout_analysis: ScoutAnalysis) -> None:
        state = ScoutAlertState(alerts_today=4)
        opps = [ScoutOpportunity(
            ticker="AAPL", analysis=scout_analysis,
            composite_score=80, is_qualified=True,
        )]
        result = filter_for_alert(opps, state)
        assert len(result) == 0

    def test_filter_dedup_ticker(self, scout_analysis: ScoutAnalysis) -> None:
        state = ScoutAlertState(alerted_tickers_today={"AAPL"})
        opps = [ScoutOpportunity(
            ticker="AAPL", analysis=scout_analysis,
            composite_score=80, is_qualified=True,
        )]
        result = filter_for_alert(opps, state)
        assert len(result) == 0

    def test_format_alert(self, scout_analysis: ScoutAnalysis) -> None:
        now = datetime.utcnow()
        opp = ScoutOpportunity(
            ticker="AAPL", analysis=scout_analysis,
            mentions=[
                RawMention("AAPL", "unusual_whales", "twitter", now, "text", "@uw"),
            ],
            composite_score=80, is_qualified=True,
        )
        output = format_scout_alert(opp)
        assert "SCOUT ALERT: AAPL" in output


# ---------------------------------------------------------------------------
# Regime Detection
# ---------------------------------------------------------------------------

class TestRegime:
    def test_attack_regime(self) -> None:
        state = classify_regime(vix=15.0, spy_change_pct=0.5)
        assert state.regime == "attack"
        assert state.severity == "normal"

    def test_hold_regime(self) -> None:
        state = classify_regime(vix=22.0, spy_change_pct=-0.005)
        assert state.regime == "hold"

    def test_defend_regime(self) -> None:
        state = classify_regime(vix=30.0, spy_change_pct=-0.01)
        assert state.regime == "defend"

    def test_crisis_from_vix(self) -> None:
        state = classify_regime(vix=40.0, spy_change_pct=-2.0)
        assert state.regime == "crisis"

    def test_crisis_from_spy_drop(self) -> None:
        # VIX says "hold" but SPY crash escalates to crisis
        state = classify_regime(vix=22.0, spy_change_pct=-0.06)
        assert state.regime == "crisis"
        assert state.severity == "crisis"

    def test_extreme_severity(self) -> None:
        state = classify_regime(vix=45.0, spy_change_pct=-9.0)
        assert state.severity == "extreme"
        assert state.regime == "crisis"

    def test_regime_change_detected(self) -> None:
        old = RegimeState("attack", 16.0, 0.5, "normal", 0.90, datetime.utcnow())
        new = classify_regime(vix=30.0, spy_change_pct=-2.0)
        assert detect_regime_change(old, new)
        assert new.changed_from == "attack"

    def test_no_regime_change(self) -> None:
        old = RegimeState("hold", 22.0, -0.005, "normal", 0.70, datetime.utcnow())
        new = classify_regime(vix=23.0, spy_change_pct=-0.003)
        assert not detect_regime_change(old, new)

    def test_format_alert(self) -> None:
        state = classify_regime(vix=36.0, spy_change_pct=-4.0)
        state.changed_from = "hold"
        output = format_regime_alert(state)
        assert "REGIME CHANGE" in output
        assert "CRISIS" in output
        assert "ESCALATION" in output


# ---------------------------------------------------------------------------
# Continuous Monitor Tripwires
# ---------------------------------------------------------------------------

class TestPriceTripwires:
    def test_intraday_dip(self) -> None:
        events = check_price_tripwires(
            "AAPL", Decimal("145"), Decimal("150"), None,
            Decimal("144"), 1000000, 500000,
        )
        dips = [e for e in events if e.event_type == "price_dip"]
        assert len(dips) == 1
        assert "down" in dips[0].message.lower()

    def test_sudden_drop(self) -> None:
        events = check_price_tripwires(
            "AAPL", Decimal("148"), Decimal("155"), Decimal("151"),
            Decimal("147"), 1000000, 500000,
        )
        drops = [e for e in events if e.event_type == "sudden_drop"]
        assert len(drops) == 1

    def test_volume_spike(self) -> None:
        events = check_price_tripwires(
            "AAPL", Decimal("149"), Decimal("150"), None,
            Decimal("148"), 1000000, 4000000,
        )
        spikes = [e for e in events if e.event_type == "volume_spike"]
        assert len(spikes) == 1

    def test_bounce_from_low(self) -> None:
        events = check_price_tripwires(
            "AAPL", Decimal("144"), Decimal("150"), None,
            Decimal("141"), 1000000, 500000,
        )
        bounces = [e for e in events if e.event_type == "bounce_from_low"]
        assert len(bounces) == 1

    def test_no_events_normal_day(self) -> None:
        events = check_price_tripwires(
            "AAPL", Decimal("151"), Decimal("150"), Decimal("150.50"),
            Decimal("149.50"), 1000000, 900000,
        )
        assert len(events) == 0


class TestIVTripwires:
    def test_iv_spike(self) -> None:
        events = check_iv_tripwires("AAPL", 65.0, 45.0)
        assert any(e.event_type == "iv_spike" for e in events)

    def test_iv_cross_50(self) -> None:
        events = check_iv_tripwires("AAPL", 55.0, 45.0)
        assert any(e.event_type == "iv_cross_50" for e in events)

    def test_iv_cross_70(self) -> None:
        events = check_iv_tripwires("AAPL", 75.0, 65.0)
        assert any(e.event_type == "iv_cross_70" for e in events)

    def test_no_events(self) -> None:
        events = check_iv_tripwires("AAPL", 45.0, 42.0)
        assert len(events) == 0


class TestPositionTripwires:
    def test_profit_target(self) -> None:
        events = check_position_tripwires(
            "AAPL", Decimal("3.50"), Decimal("1.50"),
            delta=-0.20, days_to_expiry=20,
            max_profit=Decimal("350"), current_profit=Decimal("200"),
        )
        assert any(e.event_type == "profit_target" for e in events)

    def test_loss_stop_monthly(self) -> None:
        events = check_position_tripwires(
            "AAPL", Decimal("3.50"), Decimal("7.50"),
            delta=-0.50, days_to_expiry=25,
            max_profit=Decimal("350"), current_profit=Decimal("-400"),
        )
        assert any(e.event_type == "loss_stop" for e in events)

    def test_loss_stop_weekly(self) -> None:
        events = check_position_tripwires(
            "AAPL", Decimal("3.50"), Decimal("5.50"),  # 1.57x > 1.5x weekly
            delta=-0.40, days_to_expiry=5,
            max_profit=Decimal("350"), current_profit=Decimal("-200"),
        )
        assert any(e.event_type == "loss_stop" for e in events)

    def test_delta_danger(self) -> None:
        events = check_position_tripwires(
            "AAPL", Decimal("3.50"), Decimal("5.00"),
            delta=-0.65, days_to_expiry=20,
            max_profit=Decimal("350"), current_profit=Decimal("-150"),
        )
        assert any(e.event_type == "delta_danger" for e in events)

    def test_expiry_risk(self) -> None:
        events = check_position_tripwires(
            "AAPL", Decimal("3.50"), Decimal("4.00"),
            delta=-0.45, days_to_expiry=1,
            max_profit=Decimal("350"), current_profit=Decimal("-50"),
        )
        assert any(e.event_type == "expiry_risk" for e in events)


class TestMonitorState:
    def test_can_alert(self) -> None:
        state = MonitorState()
        config = TripwireConfig()
        assert state.can_alert("AAPL", config)

    def test_daily_cap(self) -> None:
        state = MonitorState(alerts_today=8)
        config = TripwireConfig()
        assert not state.can_alert("AAPL", config)

    def test_ticker_cap(self) -> None:
        state = MonitorState(ticker_alerts_today={"AAPL": 2})
        config = TripwireConfig()
        assert not state.can_alert("AAPL", config)
        assert state.can_alert("NVDA", config)

    def test_cooldown(self) -> None:
        state = MonitorState()
        config = TripwireConfig()
        state.last_analysis["AAPL"] = datetime.utcnow()
        assert state.is_in_cooldown("AAPL", config)
        assert not state.is_in_cooldown("NVDA", config)


# ---------------------------------------------------------------------------
# Pre-Market Sentinel
# ---------------------------------------------------------------------------

class TestSentinel:
    def test_all_clear(self) -> None:
        alert = check_premarket(
            spy_futures_pct=0.003,
            nasdaq_futures_pct=0.005,
            vix_futures_change=0.5,
        )
        assert not alert.triggered
        assert alert.severity == "normal"

    def test_spy_drop_trigger(self) -> None:
        alert = check_premarket(
            spy_futures_pct=-0.025,
            nasdaq_futures_pct=-0.01,
            vix_futures_change=2.0,
        )
        assert alert.triggered
        assert alert.severity == "elevated"
        assert len(alert.triggers) == 1

    def test_emergency_multiple_triggers(self) -> None:
        alert = check_premarket(
            spy_futures_pct=-0.03,
            nasdaq_futures_pct=-0.04,
            vix_futures_change=6.0,
        )
        assert alert.triggered
        assert alert.severity == "emergency"
        assert len(alert.triggers) == 3

    def test_margin_concern(self) -> None:
        alert = check_premarket(
            spy_futures_pct=-0.03,
            nasdaq_futures_pct=-0.01,
            vix_futures_change=2.0,
            margin_utilization=0.70,
        )
        assert alert.margin_concern

    def test_weekly_exposure(self) -> None:
        alert = check_premarket(
            spy_futures_pct=-0.025,
            nasdaq_futures_pct=-0.01,
            vix_futures_change=2.0,
            has_weekly_options=True,
        )
        assert alert.weekly_options_exposed

    def test_format_clear(self) -> None:
        alert = check_premarket(0.005, 0.008, 0.2)
        output = format_sentinel_alert(alert)
        assert "All clear" in output

    def test_format_alert(self) -> None:
        alert = check_premarket(-0.03, -0.04, 7.0, True, 0.70)
        output = format_sentinel_alert(alert)
        assert "EMERGENCY" in output
        assert "MARGIN CONCERN" in output
