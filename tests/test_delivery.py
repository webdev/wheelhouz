"""Tests for src/delivery/ — telegram, briefing, onboarding."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from src.models.enums import PositionAction, PositionType, SignalType
from src.models.execution import LivePriceGate
from src.models.analysis import RiskReport
from src.models.signals import AlphaSignal

from src.delivery.telegram_bot import (
    AlertPriority,
    AlertThrottling,
    TelegramFormatter,
    format_execution_result,
    format_gated_alert,
    split_message,
)
from src.delivery.briefing import (
    format_actions,
    format_macro,
    format_opportunities,
    format_portfolio,
    format_risk,
    format_signals,
)
from src.delivery.onboarding import (
    auto_classify_portfolio,
    analyze_gaps,
    generate_transition_plan,
    format_onboarding_summary,
)

from tests.fixtures.market_data import make_market_context
from tests.fixtures.sample_portfolio import make_portfolio_state, make_position
from tests.fixtures.trades import make_alpha_signal, make_sized_opportunity


# ── Message Splitting ───────────────────────────────────────────


class TestSplitMessage:
    def test_short_message_not_split(self) -> None:
        msg = "Hello, world!"
        assert split_message(msg) == [msg]

    def test_splits_on_double_newline(self) -> None:
        msg = "A" * 2000 + "\n\n" + "B" * 2000
        chunks = split_message(msg, max_len=4096)
        assert len(chunks) == 1  # 4003 chars < 4096

    def test_splits_long_message(self) -> None:
        msg = "Line\n" * 1000  # 5000 chars
        chunks = split_message(msg, max_len=100)
        assert len(chunks) > 1
        assert all(len(c) <= 100 for c in chunks)

    def test_preserves_content(self) -> None:
        msg = "A" * 200
        chunks = split_message(msg, max_len=50)
        assert "".join(chunks) == msg

    def test_empty_message(self) -> None:
        assert split_message("") == [""]


# ── Alert Throttling ────────────────────────────────────────────


class TestAlertThrottling:
    def test_allows_first_alert(self) -> None:
        throttle = AlertThrottling()
        throttled, reason = throttle.should_throttle(
            "NVDA", AlertPriority.HIGH, now=datetime(2026, 4, 13, 10, 0),
        )
        assert not throttled

    def test_blocks_after_daily_limit(self) -> None:
        throttle = AlertThrottling(max_alerts_per_day=2)
        throttle._alerts_today = 2
        throttled, reason = throttle.should_throttle(
            "NVDA", AlertPriority.HIGH, now=datetime(2026, 4, 13, 10, 0),
        )
        assert throttled
        assert "Daily limit" in reason

    def test_blocks_after_hourly_limit(self) -> None:
        throttle = AlertThrottling(max_alerts_per_hour=1)
        throttle._alerts_this_hour = 1
        throttled, _ = throttle.should_throttle(
            "NVDA", AlertPriority.HIGH, now=datetime(2026, 4, 13, 10, 0),
        )
        assert throttled

    def test_blocks_same_ticker_limit(self) -> None:
        throttle = AlertThrottling(max_same_ticker_per_day=1)
        throttle._ticker_counts["NVDA"] = 1
        throttled, reason = throttle.should_throttle(
            "NVDA", AlertPriority.HIGH, now=datetime(2026, 4, 13, 10, 0),
        )
        assert throttled
        assert "NVDA" in reason

    def test_critical_bypasses_throttle(self) -> None:
        throttle = AlertThrottling(max_alerts_per_day=0)
        throttled, _ = throttle.should_throttle(
            "NVDA", AlertPriority.CRITICAL, now=datetime(2026, 4, 13, 10, 0),
        )
        assert not throttled

    def test_quiet_hours_block(self) -> None:
        throttle = AlertThrottling(quiet_start="11:30", quiet_end="13:00")
        throttled, reason = throttle.should_throttle(
            "NVDA", AlertPriority.HIGH, now=datetime(2026, 4, 13, 12, 0),
        )
        assert throttled
        assert "Quiet" in reason

    def test_record_alert_increments(self) -> None:
        throttle = AlertThrottling()
        throttle.record_alert("NVDA")
        assert throttle._alerts_today == 1
        assert throttle._ticker_counts["NVDA"] == 1

    def test_reset_daily(self) -> None:
        throttle = AlertThrottling()
        throttle.record_alert("NVDA")
        throttle.reset_daily()
        assert throttle._alerts_today == 0
        assert throttle._ticker_counts == {}


# ── TelegramFormatter ──────────────────────────────────────────


SAMPLE_BRIEFING = """\
━━ REGIME ━━
ATTACK | VIX 24.8

━━ SIGNAL FLASH ━━
NVDA down 3.2%, IV rank 65. AMD oversold RSI.

━━ ATTACK PLAN ━━
1. SELL 2x NVDA $800P at $12.50
2. SELL 1x AMD $120P at $3.80

━━ POSITION MANAGEMENT ━━
Close AAPL $170P at 55% profit.

━━ PORTFOLIO SCORECARD ━━
Daily theta: $380/day
YTD: +14.2%
Capital efficiency: 82%

━━ TAX ALERTS ━━
PLTR: wash sale window (closes Apr 28).
GOOG: LTCG in 47 days.

━━ GUARDRAILS ━━
ADBE at 9.6% — approaching 10% limit.
"""


class TestTelegramFormatter:
    def test_extract_section(self) -> None:
        fmt = TelegramFormatter()
        attack = fmt.extract_section(SAMPLE_BRIEFING, "ATTACK PLAN")
        assert "SELL 2x NVDA" in attack
        assert "SELL 1x AMD" in attack

    def test_extract_section_not_found(self) -> None:
        fmt = TelegramFormatter()
        assert fmt.extract_section(SAMPLE_BRIEFING, "NONEXISTENT") == ""

    def test_extract_value(self) -> None:
        fmt = TelegramFormatter()
        theta = fmt.extract_value(SAMPLE_BRIEFING, "Daily theta")
        assert theta is not None
        assert "$380" in theta

    def test_extract_value_missing(self) -> None:
        fmt = TelegramFormatter()
        assert fmt.extract_value(SAMPLE_BRIEFING, "Missing key") is None

    def test_count_trades(self) -> None:
        fmt = TelegramFormatter()
        assert fmt.count_trades(SAMPLE_BRIEFING) == 2

    def test_extract_summary(self) -> None:
        fmt = TelegramFormatter()
        summary = fmt.extract_summary(SAMPLE_BRIEFING)
        assert "trade" in summary.lower()
        assert "\n" in summary  # multi-line

    def test_format_morning_briefing_structure(self) -> None:
        fmt = TelegramFormatter()
        result = fmt.format_morning_briefing(SAMPLE_BRIEFING)
        assert "summary" in result
        assert "sections" in result
        sections = result["sections"]
        assert isinstance(sections, dict)
        assert "full_briefing" in sections  # type: ignore[operator]
        assert "trades_only" in sections  # type: ignore[operator]


# ── Gate Alert Formatting ───────────────────────────────────────


class TestGateAlerts:
    def test_format_gated_alert(self) -> None:
        gate = LivePriceGate(
            symbol="NVDA",
            trade_type="sell_put",
            strike=Decimal("800.00"),
            expiration=date.today() + timedelta(days=30),
            analysis_time=datetime.utcnow(),
            analysis_price=Decimal("875.00"),
            analysis_premium=Decimal("12.50"),
            signals=[make_alpha_signal()],
            conviction="high",
            underlying_floor=Decimal("848.75"),
            underlying_ceiling=Decimal("901.25"),
            min_premium=Decimal("10.00"),
            min_iv_rank=50.0,
        )
        mkt = make_market_context()
        text = format_gated_alert(gate, mkt)
        assert "NVDA" in text
        assert "800.00" in text
        assert "EXECUTE" in text
        assert "HIGH" in text

    def test_format_execution_valid(self) -> None:
        gate = LivePriceGate(
            symbol="NVDA",
            trade_type="sell_put",
            strike=Decimal("800.00"),
            expiration=date.today() + timedelta(days=30),
            analysis_time=datetime.utcnow(),
            analysis_price=Decimal("875.00"),
            analysis_premium=Decimal("12.50"),
        )
        result = format_execution_result(
            gate, valid=True, reason="",
            live_data={"price": 876.0, "premium": 12.80, "iv_rank": 65.0},
        )
        assert "EXECUTED" in result
        assert "NVDA" in result

    def test_format_execution_blocked(self) -> None:
        gate = LivePriceGate(
            symbol="NVDA",
            trade_type="sell_put",
            strike=Decimal("800.00"),
            expiration=date.today() + timedelta(days=30),
            analysis_time=datetime.utcnow(),
            analysis_price=Decimal("875.00"),
            analysis_premium=Decimal("12.50"),
        )
        result = format_execution_result(
            gate, valid=False, reason="Price moved 5%",
            live_data={"price": 830.0, "premium": 18.00},
        )
        assert "BLOCKED" in result
        assert "Price moved 5%" in result


# ── Briefing Format Helpers ─────────────────────────────────────


class TestBriefingFormatters:
    def test_format_signals_empty(self) -> None:
        assert format_signals([]) == "No signals fired today."

    def test_format_signals_with_data(self) -> None:
        signals = [make_alpha_signal(), make_alpha_signal(symbol="AMD")]
        text = format_signals(signals)
        assert "NVDA" in text
        assert "AMD" in text

    def test_format_portfolio(self) -> None:
        portfolio = make_portfolio_state()
        text = format_portfolio(portfolio)
        assert "NLV" in text
        assert "1,000,000" in text
        assert "theta" in text.lower()

    def test_format_opportunities_empty(self) -> None:
        assert format_opportunities([]) == "No opportunities found."

    def test_format_opportunities_with_data(self) -> None:
        opps = [make_sized_opportunity()]
        text = format_opportunities(opps)
        assert "NVDA" in text
        assert "HIGH" in text

    def test_format_risk(self) -> None:
        risk = RiskReport(
            adbe_pct=0.096,
            top_5_concentration=0.32,
            portfolio_beta=1.2,
            daily_theta=Decimal("380"),
            impact_5pct_down=Decimal("-35000"),
            margin_utilization=0.35,
            capital_efficiency=0.82,
            idle_capital_pct=0.09,
        )
        text = format_risk(risk)
        assert "ADBE" in text
        assert "9.6%" in text

    def test_format_macro(self) -> None:
        mkt = make_market_context(vix=24.8, vix_change_1d=3.2)
        text = format_macro(mkt)
        assert "VIX" in text
        assert "24.8" in text

    def test_format_actions_empty(self) -> None:
        assert format_actions([]) == "No position actions needed."

    def test_format_actions_with_data(self) -> None:
        pos = make_position()
        actions = [(pos, PositionAction.CLOSE_EARLY, "50% profit target hit")]
        text = format_actions(actions)
        assert "NVDA" in text
        assert "close_early" in text


# ── Onboarding: Auto-Classify ──────────────────────────────────


class TestAutoClassify:
    def test_classifies_options_as_engine2(self) -> None:
        portfolio = make_portfolio_state()
        intake = auto_classify_portfolio(portfolio)
        assert len(intake.short_puts) > 0
        assert all(p.position_type == "short_put" for p in intake.short_puts)

    def test_classifies_cash(self) -> None:
        portfolio = make_portfolio_state(cash_available=Decimal("100000"))
        intake = auto_classify_portfolio(portfolio)
        assert intake.cash == Decimal("100000")

    def test_adbe_suggested_engine2(self) -> None:
        portfolio = make_portfolio_state()
        intake = auto_classify_portfolio(portfolio)
        adbe = [s for s in intake.stock_positions if s.symbol == "ADBE"]
        assert len(adbe) == 1
        assert adbe[0].suggested_engine == "engine2"
        assert "RSU" in (adbe[0].suggestion_reason or "")

    def test_ltcg_stock_suggested_engine1(self) -> None:
        pos = make_position(
            position_type="long_stock",
            symbol="GOOG",
            quantity=50,
            holding_period_days=400,
            unrealized_pnl=Decimal("5000"),
            cost_basis=Decimal("150.00"),
            underlying_price=Decimal("180.00"),
            strike=Decimal("0"),
            delta=1.0, theta=0.0, gamma=0.0, vega=0.0, iv=0.0,
        )
        portfolio = make_portfolio_state(positions=[pos])
        intake = auto_classify_portfolio(portfolio)
        assert len(intake.stock_positions) == 1
        assert intake.stock_positions[0].suggested_engine == "engine1"

    def test_round_lot_recent_suggested_engine2(self) -> None:
        pos = make_position(
            position_type="long_stock",
            symbol="AMD",
            quantity=100,
            holding_period_days=30,
            unrealized_pnl=Decimal("-200"),
            cost_basis=Decimal("120.00"),
            underlying_price=Decimal("118.00"),
            strike=Decimal("0"),
            delta=1.0, theta=0.0, gamma=0.0, vega=0.0, iv=0.0,
        )
        portfolio = make_portfolio_state(positions=[pos])
        intake = auto_classify_portfolio(portfolio)
        assert intake.stock_positions[0].suggested_engine == "engine2"
        assert "assignment" in (intake.stock_positions[0].suggestion_reason or "").lower()


# ── Onboarding: Gap Analysis ───────────────────────────────────


class TestGapAnalysis:
    def test_identifies_stranded_profits(self) -> None:
        pos = make_position(profit_pct=0.85, days_to_expiry=20)
        portfolio = make_portfolio_state(positions=[pos])
        intake = auto_classify_portfolio(portfolio)
        gap = analyze_gaps(intake, portfolio)
        assert len(gap.critical_issues) >= 1
        assert any("85%" in issue for issue in gap.critical_issues)

    def test_identifies_concentration_violation(self) -> None:
        portfolio = make_portfolio_state(
            concentration={"ADBE": 0.15, "NVDA": 0.08},
        )
        intake = auto_classify_portfolio(portfolio)
        gap = analyze_gaps(intake, portfolio)
        assert any("ADBE" in issue for issue in gap.important_issues)

    def test_empty_portfolio_no_crash(self) -> None:
        portfolio = make_portfolio_state(
            positions=[], concentration={},
            cash_available=Decimal("1000000"),
        )
        intake = auto_classify_portfolio(portfolio)
        gap = analyze_gaps(intake, portfolio)
        assert gap.engine3_current_pct > 0  # all cash


# ── Onboarding: Transition Plan ────────────────────────────────


class TestTransitionPlan:
    def test_generates_immediate_for_stranded(self) -> None:
        pos = make_position(profit_pct=0.85, days_to_expiry=20)
        portfolio = make_portfolio_state(positions=[pos])
        intake = auto_classify_portfolio(portfolio)
        gap = analyze_gaps(intake, portfolio)
        plan = generate_transition_plan(intake, gap, portfolio)
        assert len(plan.immediate_actions) >= 1
        assert plan.immediate_actions[0].action == "close"

    def test_adbe_concentration_medium_term(self) -> None:
        portfolio = make_portfolio_state(
            concentration={"ADBE": 0.18},
        )
        intake = auto_classify_portfolio(portfolio)
        gap = analyze_gaps(intake, portfolio)
        plan = generate_transition_plan(intake, gap, portfolio)
        adbe_actions = [a for a in plan.medium_term_actions if a.symbol == "ADBE"]
        assert len(adbe_actions) >= 1

    def test_format_onboarding_summary(self) -> None:
        portfolio = make_portfolio_state()
        intake = auto_classify_portfolio(portfolio)
        gap = analyze_gaps(intake, portfolio)
        plan = generate_transition_plan(intake, gap, portfolio)
        summary = format_onboarding_summary(intake, gap, plan)
        assert "ONBOARDING COMPLETE" in summary
        assert "Engine 1" in summary
        assert "Engine 2" in summary
        assert "Engine 3" in summary

    def test_transition_weeks_bounded(self) -> None:
        portfolio = make_portfolio_state(positions=[])
        intake = auto_classify_portfolio(portfolio)
        gap = analyze_gaps(intake, portfolio)
        plan = generate_transition_plan(intake, gap, portfolio)
        assert 4 <= plan.estimated_transition_weeks <= 12
