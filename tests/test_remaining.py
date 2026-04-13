"""Tests for Sprints 14-17: bloodbath, correlation, DB, Alpaca, graduation, attribution, vesting."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from src.db.repository import (
    InMemoryDB,
    LearningRepository,
    SnapshotRepository,
    TradeRepository,
    WashSaleRepository,
)
from src.execution.alpaca_client import AlpacaPaperClient
from src.execution.graduation import (
    GoLiveStatus,
    determine_execution_level,
    evaluate_go_live,
    format_go_live_status,
    should_auto_execute,
)
from src.learning.attribution import (
    PerformanceAttribution,
    compute_attribution,
    format_attribution,
)
from src.monitor.bloodbath import (
    CrisisLevel,
    assess_recovery,
    calculate_crisis_fill_price,
    check_employer_crisis,
    detect_crisis_level,
    detect_sector_repricing,
    determine_crisis_actions,
    format_crisis_alert,
    project_margin_stress,
)
from src.risk.correlation import (
    analyze_correlation,
    format_correlation_report,
    would_increase_concentration,
)
from src.risk.vesting import (
    VestingTracker,
    check_employer_emergency,
    format_vesting_summary,
)


# ===========================================================================
# Bloodbath Protocol
# ===========================================================================

class TestCrisisDetection:
    def test_no_crisis(self) -> None:
        crisis = detect_crisis_level(vix=18.0, spy_drop_pct=-0.01)
        assert crisis.level == "none"

    def test_elevated(self) -> None:
        crisis = detect_crisis_level(vix=26.0, spy_drop_pct=-0.01)
        assert crisis.level == "elevated"

    def test_severe(self) -> None:
        crisis = detect_crisis_level(vix=32.0, spy_drop_pct=-0.04)
        assert crisis.level == "severe"

    def test_crisis(self) -> None:
        crisis = detect_crisis_level(vix=36.0, spy_drop_pct=-0.06)
        assert crisis.level == "crisis"

    def test_extreme(self) -> None:
        crisis = detect_crisis_level(vix=50.0, spy_drop_pct=-0.09)
        assert crisis.level == "extreme"

    def test_spy_drop_escalates(self) -> None:
        # VIX says elevated, but SPY drop says crisis
        crisis = detect_crisis_level(vix=26.0, spy_drop_pct=-0.06)
        assert crisis.level == "crisis"


class TestCrisisActions:
    def test_crisis_closes_weeklies(self) -> None:
        crisis = CrisisLevel(level="crisis", vix=36.0, spy_drop_pct=-0.06)
        actions = determine_crisis_actions(
            crisis,
            weekly_positions=[{"symbol": "AAPL"}, {"symbol": "NVDA"}],
            pending_orders=3,
            margin_utilization=0.50,
        )
        close_actions = [a for a in actions if a.action == "close_weeklies"]
        assert len(close_actions) == 2
        assert any(a.action == "cancel_pending" for a in actions)
        assert any(a.action == "block_new" for a in actions)

    def test_margin_stress_action(self) -> None:
        crisis = CrisisLevel(level="severe", vix=32.0, spy_drop_pct=-0.04)
        actions = determine_crisis_actions(crisis, [], 0, margin_utilization=0.70)
        assert any(a.action == "preemptive_close" for a in actions)

    def test_no_action_elevated(self) -> None:
        crisis = CrisisLevel(level="elevated", vix=26.0, spy_drop_pct=-0.02)
        actions = determine_crisis_actions(crisis, [], 0, 0.30)
        assert len(actions) == 0


class TestCrisisFillPrice:
    def test_mid_first_30s(self) -> None:
        price = calculate_crisis_fill_price(
            Decimal("5.00"), Decimal("3.00"), Decimal("7.00"), 15,
        )
        assert price == Decimal("5.00")

    def test_walk_toward_bid(self) -> None:
        price = calculate_crisis_fill_price(
            Decimal("5.00"), Decimal("3.00"), Decimal("7.00"), 60,
        )
        assert price < Decimal("5.00")
        assert price >= Decimal("3.00")

    def test_market_after_2min(self) -> None:
        price = calculate_crisis_fill_price(
            Decimal("5.00"), Decimal("3.00"), Decimal("7.00"), 150,
        )
        assert price == Decimal("3.00")


class TestMarginStress:
    def test_no_call(self) -> None:
        projected, call = project_margin_stress(0.40, 1.2, -0.03)
        assert not call

    def test_margin_call(self) -> None:
        projected, call = project_margin_stress(0.80, 1.5, -0.10)
        assert call


class TestSectorRepricing:
    def test_sector_specific(self) -> None:
        changes = {"AAPL": -0.08, "NVDA": -0.12, "JPM": 0.02, "XOM": 0.03}
        result = detect_sector_repricing(changes, spy_change=-0.02)
        assert result.is_sector_specific
        assert result.divergence_pct > 0.08

    def test_broad_sell(self) -> None:
        changes = {"AAPL": -0.04, "NVDA": -0.05, "JPM": -0.03, "XOM": -0.04}
        result = detect_sector_repricing(changes, spy_change=-0.04)
        assert not result.is_sector_specific


class TestEmployerCrisis:
    def test_triggered(self) -> None:
        alert = check_employer_crisis(adbe_change_5d_pct=-0.22, adbe_nlv_pct=0.25)
        assert alert.triggered
        assert alert.action == "sell_50pct"

    def test_over_target(self) -> None:
        alert = check_employer_crisis(adbe_change_5d_pct=-0.05, adbe_nlv_pct=0.20)
        assert not alert.triggered
        assert alert.action == "sell_to_target"

    def test_within_target(self) -> None:
        alert = check_employer_crisis(adbe_change_5d_pct=0.02, adbe_nlv_pct=0.10)
        assert alert.action == "none"


class TestRecovery:
    def test_ready(self) -> None:
        result = assess_recovery(
            ["vix_peak", "volume_climax", "breadth_improvement", "sector_leadership"],
            dry_powder_pct=0.10,
        )
        assert result.ready_for_recovery
        assert result.deployment_pct > 0
        assert "monthly puts" in result.strategy.lower()

    def test_not_ready(self) -> None:
        result = assess_recovery(["vix_peak", "volume_climax"], dry_powder_pct=0.10)
        assert not result.ready_for_recovery
        assert result.deployment_pct == 0

    def test_invalid_signals_ignored(self) -> None:
        result = assess_recovery(
            ["vix_peak", "made_up_signal", "volume_climax"],
            dry_powder_pct=0.10,
        )
        assert len(result.signals_present) == 2

    def test_format_alert(self) -> None:
        crisis = detect_crisis_level(vix=40.0, spy_drop_pct=-0.07)
        actions = determine_crisis_actions(crisis, [{"symbol": "AAPL"}], 2, 0.50)
        output = format_crisis_alert(crisis, actions)
        assert "BLOODBATH" in output


# ===========================================================================
# Correlation
# ===========================================================================

class TestCorrelation:
    def test_normal_analysis(self) -> None:
        positions = {
            "AAPL": Decimal("80000"),
            "NVDA": Decimal("60000"),
            "JPM": Decimal("50000"),
        }
        report = analyze_correlation(positions, Decimal("1000000"), vix=18.0)
        assert not report.crisis_mode
        assert report.max_single_name == "AAPL"

    def test_crisis_mode(self) -> None:
        positions = {
            "AAPL": Decimal("150000"),
            "NVDA": Decimal("150000"),
            "MSFT": Decimal("100000"),
        }
        report = analyze_correlation(positions, Decimal("1000000"), vix=35.0)
        assert report.crisis_mode
        assert any("CRISIS" in w for w in report.warnings)

    def test_single_name_violation(self) -> None:
        positions = {"AAPL": Decimal("120000")}
        report = analyze_correlation(positions, Decimal("1000000"), vix=18.0)
        assert any("AAPL" in w and "12.0%" in w for w in report.warnings)

    def test_sector_violation(self) -> None:
        positions = {
            "AAPL": Decimal("100000"),
            "MSFT": Decimal("100000"),
            "GOOGL": Decimal("100000"),
            "META": Decimal("80000"),
        }
        report = analyze_correlation(positions, Decimal("1000000"), vix=18.0)
        assert any("technology" in w for w in report.warnings)

    def test_would_increase_concentration(self) -> None:
        positions = {"AAPL": Decimal("90000")}
        violates, reason = would_increase_concentration(
            "AAPL", Decimal("20000"), positions, Decimal("1000000"),
        )
        assert violates
        assert "11.0%" in reason

    def test_no_violation(self) -> None:
        positions = {"AAPL": Decimal("50000")}
        violates, _ = would_increase_concentration(
            "AAPL", Decimal("10000"), positions, Decimal("1000000"),
        )
        assert not violates

    def test_format_report(self) -> None:
        positions = {"AAPL": Decimal("80000"), "NVDA": Decimal("60000")}
        report = analyze_correlation(positions, Decimal("1000000"), vix=18.0)
        output = format_correlation_report(report)
        assert "CORRELATION" in output


# ===========================================================================
# Database Repository
# ===========================================================================

class TestInMemoryDB:
    def test_insert_and_find(self) -> None:
        db = InMemoryDB()
        row_id = db.insert("test", {"name": "foo", "value": 42})
        assert row_id == 1
        result = db.find_one("test", name="foo")
        assert result is not None
        assert result["value"] == 42

    def test_update(self) -> None:
        db = InMemoryDB()
        row_id = db.insert("test", {"name": "bar"})
        db.update("test", row_id, name="baz")
        result = db.find_one("test", name="baz")
        assert result is not None

    def test_count(self) -> None:
        db = InMemoryDB()
        db.insert("test", {"type": "a"})
        db.insert("test", {"type": "a"})
        db.insert("test", {"type": "b"})
        assert db.count("test", type="a") == 2
        assert db.count("test") == 3


class TestTradeRepository:
    def test_log_and_retrieve(self) -> None:
        db = InMemoryDB()
        repo = TradeRepository(db)
        trade_id = repo.log_paper_trade(
            symbol="AAPL", trade_type="sell_put",
            strike=Decimal("170"), expiration=date.today() + timedelta(days=30),
            contracts=2, conviction="high", strategy="monthly_put",
            entry_price=Decimal("3.50"), entry_underlying=Decimal("175"),
            iv_rank=55.0, capital_at_risk=Decimal("34000"),
        )
        assert trade_id == 1
        open_trades = repo.get_open_paper_trades()
        assert len(open_trades) == 1

    def test_close_trade(self) -> None:
        db = InMemoryDB()
        repo = TradeRepository(db)
        trade_id = repo.log_paper_trade(
            symbol="AAPL", trade_type="sell_put",
            strike=Decimal("170"), expiration=None,
            contracts=1, conviction="medium", strategy="weekly_put",
            entry_price=Decimal("2.00"), entry_underlying=Decimal("175"),
            iv_rank=60.0, capital_at_risk=Decimal("17000"),
        )
        repo.close_paper_trade(
            trade_id, exit_price=Decimal("0.50"),
            exit_underlying=Decimal("178"), exit_reason="profit_target",
            pnl=Decimal("150"), pnl_pct=0.88,
        )
        assert len(repo.get_open_paper_trades()) == 0
        assert len(repo.get_closed_paper_trades()) == 1


class TestWashSaleRepo:
    def test_record_and_check(self) -> None:
        db = InMemoryDB()
        repo = WashSaleRepository(db)
        repo.record_loss("AAPL", date.today(), Decimal("500"))
        assert repo.is_blocked("AAPL")
        assert not repo.is_blocked("NVDA")


# ===========================================================================
# Alpaca Client
# ===========================================================================

class TestAlpacaClient:
    def test_submit_and_fill(self) -> None:
        client = AlpacaPaperClient()
        order = client.sell_to_open_option(
            underlying="AAPL", expiration=date(2026, 5, 15),
            option_type="put", strike=Decimal("170"),
            quantity=2, limit_price=Decimal("3.50"),
        )
        assert order.status == "filled"
        assert order.filled_price == Decimal("3.50")
        positions = client.get_positions()
        assert len(positions) == 1

    def test_cancel_order(self) -> None:
        client = AlpacaPaperClient()
        order = client.sell_to_open_option(
            underlying="AAPL", expiration=date(2026, 5, 15),
            option_type="put", strike=Decimal("170"),
            quantity=1, limit_price=Decimal("3.00"),
        )
        assert not client.cancel_order(order.order_id)  # already filled

    def test_cancel_all(self) -> None:
        client = AlpacaPaperClient()
        count = client.cancel_all_orders()
        assert count == 0

    def test_close_position(self) -> None:
        client = AlpacaPaperClient()
        client.sell_to_open_option(
            underlying="OPT", expiration=date(2026, 5, 15),
            option_type="put", strike=Decimal("100"),
            quantity=2, limit_price=Decimal("3.00"),
        )
        client.buy_to_close_option(
            underlying="OPT", expiration=date(2026, 5, 15),
            option_type="put", strike=Decimal("100"),
            quantity=2, limit_price=Decimal("1.50"),
        )
        assert len(client.get_positions()) == 0

    def test_account_state(self) -> None:
        client = AlpacaPaperClient()
        acct = client.get_account()
        assert acct.equity == Decimal("100000")


# ===========================================================================
# Graduation
# ===========================================================================

class TestGoLive:
    def test_ready(self) -> None:
        status = evaluate_go_live(
            total_trades=65, win_rate=0.58,
            high_conviction_wr=0.68, max_drawdown_pct=0.08,
            loss_stops_triggered=4,
        )
        assert status.ready
        assert len(status.blockers) == 0

    def test_not_ready(self) -> None:
        status = evaluate_go_live(
            total_trades=30, win_rate=0.48,
            high_conviction_wr=0.55, max_drawdown_pct=0.15,
            loss_stops_triggered=1,
        )
        assert not status.ready
        assert len(status.blockers) == 5

    def test_partial_ready(self) -> None:
        status = evaluate_go_live(
            total_trades=65, win_rate=0.58,
            high_conviction_wr=0.68, max_drawdown_pct=0.08,
            loss_stops_triggered=1,  # not enough
        )
        assert not status.ready
        assert len(status.blockers) == 1
        assert "loss stops" in status.blockers[0].lower()


class TestExecutionLevels:
    def test_month_1(self) -> None:
        level = determine_execution_level(1)
        assert level.level == 1
        assert level.requires_manual_approval

    def test_month_3(self) -> None:
        level = determine_execution_level(3)
        assert level.auto_close_winners
        assert level.requires_manual_approval

    def test_month_5(self) -> None:
        level = determine_execution_level(5)
        assert level.auto_execute_high
        assert level.auto_execute_medium

    def test_auto_execute_high(self) -> None:
        level = determine_execution_level(4)
        auto, reason = should_auto_execute("high", level)
        assert auto
        auto, reason = should_auto_execute("medium", level)
        assert not auto

    def test_format_status(self) -> None:
        status = evaluate_go_live(65, 0.58, 0.68, 0.08, 4)
        output = format_go_live_status(status)
        assert "GO-LIVE CHECKLIST" in output
        assert "READY" in output


# ===========================================================================
# Attribution
# ===========================================================================

class TestAttribution:
    def test_basic_attribution(self) -> None:
        trades = [
            {"strategy": "monthly_put", "signal_type": "iv_rank_spike",
             "conviction": "high", "engine": "engine2", "pnl_pct": 1.5,
             "is_winner": True, "is_scout_pick": False},
            {"strategy": "monthly_put", "signal_type": "oversold_rsi",
             "conviction": "medium", "engine": "engine2", "pnl_pct": -0.8,
             "is_winner": False, "is_scout_pick": False},
            {"strategy": "weekly_put", "signal_type": "intraday_dip",
             "conviction": "high", "engine": "engine2", "pnl_pct": 2.0,
             "is_winner": True, "is_scout_pick": True},
        ]
        attr = compute_attribution(trades)
        assert attr.blended_return == pytest.approx(0.9, abs=0.01)
        assert "monthly_put" in attr.strategy_returns
        assert attr.high_conviction_return > 0

    def test_empty_trades(self) -> None:
        attr = compute_attribution([])
        assert attr.blended_return == 0.0

    def test_scout_tracking(self) -> None:
        trades = [
            {"strategy": "put", "signal_type": "dip", "conviction": "high",
             "engine": "e2", "pnl_pct": 3.0, "is_winner": True,
             "is_scout_pick": True},
            {"strategy": "put", "signal_type": "dip", "conviction": "high",
             "engine": "e2", "pnl_pct": 1.0, "is_winner": True,
             "is_scout_pick": False},
        ]
        attr = compute_attribution(trades)
        assert attr.scout_pick_return == 3.0
        assert attr.scout_vs_watchlist == 2.0

    def test_format(self) -> None:
        trades = [
            {"strategy": "put", "signal_type": "dip", "conviction": "high",
             "engine": "engine2", "pnl_pct": 1.0, "is_winner": True,
             "is_scout_pick": False},
        ]
        attr = compute_attribution(trades)
        output = format_attribution(attr)
        assert "PERFORMANCE ATTRIBUTION" in output
        assert "BY ENGINE" in output


# ===========================================================================
# Vesting
# ===========================================================================

class TestVesting:
    def test_upcoming_events(self) -> None:
        tracker = VestingTracker()
        tracker.add_event(
            date.today() + timedelta(days=30), "rsu", 50, Decimal("500"),
        )
        tracker.add_event(
            date.today() + timedelta(days=120), "espp", 100, Decimal("450"),
        )
        upcoming = tracker.get_upcoming(90)
        assert len(upcoming) == 1  # only the 30-day one

    def test_sell_plan(self) -> None:
        tracker = VestingTracker(target_concentration=0.15)
        plan = tracker.generate_sell_plan(
            current_shares=500,
            current_price=Decimal("500"),
            nlv=Decimal("1000000"),
        )
        assert plan.current_pct == 0.25
        assert plan.shares_to_sell > 0
        assert plan.quarterly_sell_shares > 0

    def test_within_target(self) -> None:
        tracker = VestingTracker(target_concentration=0.15)
        plan = tracker.generate_sell_plan(
            current_shares=200,
            current_price=Decimal("500"),
            nlv=Decimal("1000000"),
        )
        assert plan.shares_to_sell == 0

    def test_emergency(self) -> None:
        triggered, reason = check_employer_emergency("ADBE", -0.22, 0.20)
        assert triggered
        assert "emergency" in reason.lower()

    def test_no_emergency(self) -> None:
        triggered, _ = check_employer_emergency("ADBE", -0.05, 0.12)
        assert not triggered

    def test_format_summary(self) -> None:
        tracker = VestingTracker()
        tracker.add_event(
            date.today() + timedelta(days=45), "rsu", 50, Decimal("500"),
        )
        plan = tracker.generate_sell_plan(300, Decimal("500"), Decimal("1000000"))
        output = format_vesting_summary(tracker, plan)
        assert "VESTING TRACKER" in output
        assert "RSU" in output
