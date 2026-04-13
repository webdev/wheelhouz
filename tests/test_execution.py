"""Tests for src/execution/ — paper trading, gate validation, orders."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest

from src.models.execution import LivePriceGate
from src.models.paper import ExecutionRules, PaperPosition

from src.execution.paper_trader import PaperTrader
from src.execution.gate import validate_gate
from src.execution.orders import (
    calculate_smart_limit,
    estimate_fill_cost,
    is_in_trading_window,
    is_spread_acceptable,
)

from tests.fixtures.trades import make_alpha_signal, make_sized_opportunity


def _make_gate(**overrides: object) -> LivePriceGate:
    """Create a LivePriceGate with sensible defaults."""
    defaults = {
        "symbol": "NVDA",
        "trade_type": "sell_put",
        "strike": Decimal("800.00"),
        "expiration": date.today() + timedelta(days=30),
        "analysis_time": datetime.utcnow(),
        "analysis_price": Decimal("875.00"),
        "analysis_premium": Decimal("12.50"),
        "signals": [make_alpha_signal()],
        "conviction": "high",
        "underlying_floor": Decimal("848.75"),
        "underlying_ceiling": Decimal("901.25"),
        "min_premium": Decimal("10.00"),
        "min_iv_rank": 50.0,
        "max_abs_delta": 0.45,
        "max_age_hours": 8.0,
    }
    defaults.update(overrides)
    return LivePriceGate(**defaults)  # type: ignore[arg-type]


# ── Paper Trader ────────────────────────────────────────────────


class TestPaperTraderOpen:
    def test_open_trade(self) -> None:
        trader = PaperTrader(initial_capital=Decimal("100000"))
        gate = _make_gate()
        sized = make_sized_opportunity(contracts=2, capital_deployed=Decimal("160000"))

        pos = trader.open_trade(gate, sized)
        assert pos.symbol == "NVDA"
        assert pos.contracts == 2
        assert pos.conviction == "high"
        assert len(trader.open_positions) == 1
        assert trader.buying_power < trader.initial_capital

    def test_open_trade_custom_fill(self) -> None:
        trader = PaperTrader()
        gate = _make_gate()
        sized = make_sized_opportunity()
        pos = trader.open_trade(gate, sized, fill_price=Decimal("12.00"))
        assert pos.entry_price == Decimal("12.00")


class TestPaperTraderUpdate:
    def test_profit_target_triggers(self) -> None:
        trader = PaperTrader()
        gate = _make_gate()
        sized = make_sized_opportunity(contracts=1)
        pos = trader.open_trade(gate, sized)

        # Simulate option price dropping to 50% of entry (50% profit)
        reason = trader.update_position(
            pos,
            current_option_price=pos.entry_price / 2,
            current_underlying=Decimal("890.00"),
        )
        assert reason == "profit_target_50pct"

    def test_loss_stop_monthly(self) -> None:
        trader = PaperTrader()
        gate = _make_gate(expiration=date.today() + timedelta(days=30))
        sized = make_sized_opportunity(contracts=1)
        pos = trader.open_trade(gate, sized)

        # 2x loss on a monthly
        reason = trader.update_position(
            pos,
            current_option_price=pos.entry_price * 2,
            current_underlying=Decimal("780.00"),
        )
        assert reason is not None
        assert "loss_stop_2.0x" in reason

    def test_loss_stop_weekly(self) -> None:
        trader = PaperTrader()
        gate = _make_gate(expiration=date.today() + timedelta(days=5))
        sized = make_sized_opportunity(contracts=1)
        pos = trader.open_trade(gate, sized)

        # 1.5x loss on a weekly
        reason = trader.update_position(
            pos,
            current_option_price=pos.entry_price * Decimal("1.5"),
            current_underlying=Decimal("790.00"),
        )
        assert reason is not None
        assert "loss_stop_1.5x" in reason

    def test_expiry_otm(self) -> None:
        trader = PaperTrader()
        gate = _make_gate(expiration=date.today())
        sized = make_sized_opportunity(contracts=1)
        pos = trader.open_trade(gate, sized)

        reason = trader.update_position(
            pos,
            current_option_price=Decimal("0.05"),
            current_underlying=Decimal("850.00"),  # above 800 strike
        )
        assert reason == "expired_worthless"

    def test_expiry_itm(self) -> None:
        trader = PaperTrader()
        gate = _make_gate(expiration=date.today())
        sized = make_sized_opportunity(contracts=1)
        pos = trader.open_trade(gate, sized)

        # Price slightly above entry (ITM but not at loss-stop levels)
        reason = trader.update_position(
            pos,
            current_option_price=pos.entry_price + Decimal("1.00"),
            current_underlying=Decimal("795.00"),  # below 800 strike
        )
        assert reason == "assigned"

    def test_monitor_when_healthy(self) -> None:
        trader = PaperTrader()
        gate = _make_gate(expiration=date.today() + timedelta(days=25))
        sized = make_sized_opportunity(contracts=1)
        pos = trader.open_trade(gate, sized)

        reason = trader.update_position(
            pos,
            current_option_price=pos.entry_price * Decimal("0.8"),  # small profit
            current_underlying=Decimal("870.00"),
        )
        assert reason is None  # no action needed


class TestPaperTraderClose:
    def test_close_updates_capital(self) -> None:
        trader = PaperTrader(initial_capital=Decimal("100000"))
        gate = _make_gate()
        sized = make_sized_opportunity(
            contracts=1, capital_deployed=Decimal("80000"),
        )
        pos = trader.open_trade(gate, sized)
        pos.current_price = pos.entry_price / 2
        pos.current_pnl = pos.entry_price * 50  # simplified

        trader.close_position(pos, "profit_target_50pct")
        assert len(trader.open_positions) == 0
        assert len(trader.closed_positions) == 1
        assert trader.closed_positions[0].exit_reason == "profit_target_50pct"

    def test_close_deducts_commission(self) -> None:
        trader = PaperTrader()
        gate = _make_gate()
        sized = make_sized_opportunity(contracts=2)
        pos = trader.open_trade(gate, sized)
        pos.current_pnl = Decimal("500.00")

        trader.close_position(pos, "profit_target")
        # final_pnl = current_pnl - commission
        commission = trader.rules.commission_per_contract * 2
        assert pos.final_pnl == Decimal("500.00") - commission


class TestPaperTraderDashboard:
    def _make_trader_with_trades(self, n_winners: int, n_losers: int) -> PaperTrader:
        trader = PaperTrader(initial_capital=Decimal("100000"))
        for i in range(n_winners):
            pos = PaperPosition(
                symbol="NVDA",
                trade_type="sell_put",
                strike=Decimal("800"),
                expiration=date.today(),
                entry_price=Decimal("10"),
                entry_time=datetime.utcnow() - timedelta(days=30 - i),
                contracts=1,
                conviction="high" if i % 2 == 0 else "medium",
                current_pnl=Decimal("500"),
                exit_price=Decimal("5"),
                exit_time=datetime.utcnow() - timedelta(days=15 - i),
                exit_reason="profit_target_50pct",
                final_pnl=Decimal("500"),
            )
            trader.closed_positions.append(pos)

        for i in range(n_losers):
            pos = PaperPosition(
                symbol="AMD",
                trade_type="sell_put",
                strike=Decimal("120"),
                expiration=date.today(),
                entry_price=Decimal("3"),
                entry_time=datetime.utcnow() - timedelta(days=30 - i),
                contracts=1,
                conviction="low",
                current_pnl=Decimal("-300"),
                exit_price=Decimal("6"),
                exit_time=datetime.utcnow() - timedelta(days=10 - i),
                exit_reason="loss_stop_2.0x",
                final_pnl=Decimal("-300"),
            )
            trader.closed_positions.append(pos)

        return trader

    def test_empty_dashboard(self) -> None:
        trader = PaperTrader()
        dash = trader.generate_dashboard()
        assert dash.total_trades == 0
        assert not dash.ready_for_live

    def test_win_rate_calculation(self) -> None:
        trader = self._make_trader_with_trades(7, 3)
        dash = trader.generate_dashboard()
        assert dash.total_trades == 10
        assert dash.win_rate == 0.7
        assert dash.winners == 7
        assert dash.losers == 3

    def test_profit_factor(self) -> None:
        trader = self._make_trader_with_trades(5, 5)
        dash = trader.generate_dashboard()
        # 5 * 500 = 2500 winners, 5 * 300 = 1500 losers
        assert dash.profit_factor == pytest.approx(2500 / 1500, rel=0.01)

    def test_conviction_breakdown(self) -> None:
        trader = self._make_trader_with_trades(6, 4)
        dash = trader.generate_dashboard()
        assert dash.high_trades + dash.medium_trades + dash.low_trades == 10

    def test_go_live_not_ready_few_trades(self) -> None:
        trader = self._make_trader_with_trades(5, 2)
        dash = trader.generate_dashboard()
        assert not dash.has_60_trades
        assert not dash.ready_for_live

    def test_format_dashboard_empty(self) -> None:
        trader = PaperTrader()
        text = trader.format_dashboard()
        assert "No paper trades" in text

    def test_format_dashboard_with_trades(self) -> None:
        trader = self._make_trader_with_trades(7, 3)
        text = trader.format_dashboard()
        assert "PAPER TRADING DASHBOARD" in text
        assert "Win rate" in text
        assert "GO-LIVE CHECKLIST" in text

    def test_snapshot(self) -> None:
        trader = self._make_trader_with_trades(5, 2)
        snap = trader.take_snapshot()
        assert snap.trades_to_date == 7
        assert snap.win_rate == pytest.approx(5 / 7, rel=0.01)


# ── Gate Validation ─────────────────────────────────────────────


class TestGateValidation:
    def test_all_checks_pass(self) -> None:
        gate = _make_gate()
        result = validate_gate(
            gate,
            live_price=Decimal("875.00"),
            live_premium=Decimal("12.00"),
            live_iv_rank=65.0,
            live_delta=-0.25,
            live_bid=Decimal("11.80"),
            live_ask=Decimal("12.20"),
        )
        assert result.is_valid
        assert len(result.checks_failed) == 0
        assert len(result.checks_passed) >= 6

    def test_price_out_of_range(self) -> None:
        gate = _make_gate()
        result = validate_gate(
            gate,
            live_price=Decimal("920.00"),  # above ceiling
            live_premium=Decimal("12.00"),
            live_iv_rank=65.0,
            live_delta=-0.25,
            live_bid=Decimal("11.80"),
            live_ask=Decimal("12.20"),
        )
        assert not result.is_valid
        assert any("outside range" in f for f in result.checks_failed)

    def test_premium_too_low(self) -> None:
        gate = _make_gate(min_premium=Decimal("10.00"))
        result = validate_gate(
            gate,
            live_price=Decimal("875.00"),
            live_premium=Decimal("8.00"),  # below min
            live_iv_rank=65.0,
            live_delta=-0.25,
            live_bid=Decimal("7.80"),
            live_ask=Decimal("8.20"),
        )
        assert not result.is_valid
        assert any("Premium" in f for f in result.checks_failed)

    def test_iv_rank_too_low(self) -> None:
        gate = _make_gate(min_iv_rank=50.0)
        result = validate_gate(
            gate,
            live_price=Decimal("875.00"),
            live_premium=Decimal("12.00"),
            live_iv_rank=35.0,  # below min
            live_delta=-0.25,
            live_bid=Decimal("11.80"),
            live_ask=Decimal("12.20"),
        )
        assert not result.is_valid
        assert any("IV rank" in f for f in result.checks_failed)

    def test_delta_too_high(self) -> None:
        gate = _make_gate(max_abs_delta=0.35)
        result = validate_gate(
            gate,
            live_price=Decimal("875.00"),
            live_premium=Decimal("12.00"),
            live_iv_rank=65.0,
            live_delta=-0.50,  # exceeds max
            live_bid=Decimal("11.80"),
            live_ask=Decimal("12.20"),
        )
        assert not result.is_valid
        assert any("Delta" in f for f in result.checks_failed)

    def test_disqualifying_event(self) -> None:
        gate = _make_gate()
        result = validate_gate(
            gate,
            live_price=Decimal("875.00"),
            live_premium=Decimal("12.00"),
            live_iv_rank=65.0,
            live_delta=-0.25,
            live_bid=Decimal("11.80"),
            live_ask=Decimal("12.20"),
            disqualifying_events=["Earnings tomorrow"],
        )
        assert not result.is_valid
        assert any("Disqualifying" in f for f in result.checks_failed)

    def test_wide_spread(self) -> None:
        gate = _make_gate()
        result = validate_gate(
            gate,
            live_price=Decimal("875.00"),
            live_premium=Decimal("12.00"),
            live_iv_rank=65.0,
            live_delta=-0.25,
            live_bid=Decimal("10.00"),
            live_ask=Decimal("14.00"),  # 33% spread
        )
        assert not result.is_valid
        assert any("Spread" in f for f in result.checks_failed)

    def test_market_closed(self) -> None:
        gate = _make_gate()
        result = validate_gate(
            gate,
            live_price=Decimal("875.00"),
            live_premium=Decimal("12.00"),
            live_iv_rank=65.0,
            live_delta=-0.25,
            live_bid=Decimal("11.80"),
            live_ask=Decimal("12.20"),
            market_open=False,
        )
        assert not result.is_valid
        assert any("closed" in f.lower() for f in result.checks_failed)


# ── Orders ──────────────────────────────────────────────────────


class TestSmartLimit:
    def test_sell_below_mid(self) -> None:
        price = calculate_smart_limit(
            Decimal("11.80"), Decimal("12.20"), "sell",
        )
        mid = (Decimal("11.80") + Decimal("12.20")) / 2
        assert price == mid - Decimal("0.01")

    def test_buy_above_mid(self) -> None:
        price = calculate_smart_limit(
            Decimal("5.00"), Decimal("5.40"), "buy",
        )
        mid = (Decimal("5.00") + Decimal("5.40")) / 2
        assert price == mid + Decimal("0.01")


class TestSpreadCheck:
    def test_tight_spread_acceptable(self) -> None:
        ok, pct = is_spread_acceptable(Decimal("11.90"), Decimal("12.10"))
        assert ok
        assert pct < 0.05

    def test_wide_spread_rejected(self) -> None:
        ok, pct = is_spread_acceptable(Decimal("10.00"), Decimal("14.00"))
        assert not ok
        assert pct > 0.05


class TestTradingWindow:
    def test_within_window(self) -> None:
        ok, reason = is_in_trading_window(time(10, 30))
        assert ok

    def test_before_open(self) -> None:
        ok, reason = is_in_trading_window(time(9, 0))
        assert not ok
        assert "closed" in reason.lower()

    def test_first_15_min(self) -> None:
        ok, reason = is_in_trading_window(time(9, 35))
        assert not ok
        assert "first 15" in reason.lower()

    def test_last_15_min(self) -> None:
        ok, reason = is_in_trading_window(time(15, 50))
        assert not ok
        assert "last 15" in reason.lower()


class TestFillCost:
    def test_estimates_cost(self) -> None:
        result = estimate_fill_cost(
            contracts=2,
            premium=Decimal("12.50"),
        )
        assert result["gross"] == Decimal("2500.00")
        assert result["commission"] > 0
        assert result["slippage"] > 0
        assert result["net"] < result["gross"]
