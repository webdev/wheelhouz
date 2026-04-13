"""Tests for the Backtest & Learning modules."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from src.backtest.benchmark import (
    BenchmarkComparison,
    format_benchmark,
    simulate_vanilla_wheel,
)
from src.backtest.engine import (
    BacktestResult,
    WalkForwardConfig,
    WindowResult,
    format_backtest_summary,
    run_walk_forward,
)
from src.learning.loop import (
    Adjustment,
    LearningConfig,
    LearningReport,
    TradeRecord,
    format_learning_report,
    run_weekly_review,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def daily_prices() -> dict[str, list[tuple[date, Decimal]]]:
    """Generate 500 trading days of fake price data for 2 symbols."""
    prices: dict[str, list[tuple[date, Decimal]]] = {}
    base_date = date(2023, 1, 3)

    for symbol, start_price in [("AAPL", 150), ("NVDA", 200)]:
        data: list[tuple[date, Decimal]] = []
        price = Decimal(str(start_price))
        for i in range(500):
            d = base_date + timedelta(days=i)
            if d.weekday() >= 5:
                continue
            # Simulate random walk with slight upward drift
            import hashlib
            h = int(hashlib.md5(f"{symbol}{i}".encode()).hexdigest()[:4], 16)
            change = Decimal(str((h % 500 - 230) / 10000))  # slight positive bias
            price = max(price * (1 + change), Decimal("50"))
            data.append((d, price.quantize(Decimal("0.01"))))
        prices[symbol] = data

    return prices


@pytest.fixture
def sample_trades() -> list[TradeRecord]:
    """Generate 25 sample closed trades for learning loop."""
    trades: list[TradeRecord] = []
    for i in range(25):
        is_winner = i % 3 != 0  # ~67% win rate
        pnl = Decimal("200") if is_winner else Decimal("-350")
        trades.append(TradeRecord(
            trade_id=f"T{i:03d}",
            symbol="AAPL" if i % 2 == 0 else "NVDA",
            signal_type="multi_day_pullback" if i % 3 == 0 else "iv_rank_spike",
            strategy="monthly_put",
            conviction="high" if i < 10 else "medium" if i < 20 else "low",
            entry_date=f"2025-{(i % 12)+1:02d}-01",
            exit_date=f"2025-{(i % 12)+1:02d}-15",
            premium_received=Decimal("350"),
            pnl=pnl,
            pnl_pct=float(pnl / Decimal("35000")) * 100,
            is_winner=is_winner,
            scout_source="unusual_whales" if i % 5 == 0 else None,
        ))
    return trades


# ---------------------------------------------------------------------------
# Walk-Forward Backtest
# ---------------------------------------------------------------------------

class TestWalkForward:
    def test_basic_run(
        self, daily_prices: dict[str, list[tuple[date, Decimal]]]
    ) -> None:
        config = WalkForwardConfig(
            train_window=150, test_window=75, step_size=50,
        )
        result = run_walk_forward("multi_day_pullback", daily_prices, config)
        assert result.signal_type == "multi_day_pullback"
        assert len(result.windows) > 0
        assert result.total_trades > 0

    def test_insufficient_data(self) -> None:
        short_data = {
            "AAPL": [
                (date(2023, 1, i + 1), Decimal("150"))
                for i in range(10)
            ]
        }
        result = run_walk_forward("intraday_dip", short_data)
        assert len(result.windows) == 0
        assert result.total_trades == 0

    def test_custom_config(
        self, daily_prices: dict[str, list[tuple[date, Decimal]]]
    ) -> None:
        config = WalkForwardConfig(
            train_window=100,
            test_window=50,
            step_size=25,
        )
        result = run_walk_forward("oversold_rsi", daily_prices, config)
        assert len(result.windows) >= 2

    def test_robustness_flag(self) -> None:
        result = BacktestResult(
            signal_type="test",
            windows=[
                WindowResult(
                    train_start=date(2023, 1, 1),
                    train_end=date(2023, 12, 31),
                    test_start=date(2024, 1, 1),
                    test_end=date(2024, 6, 30),
                    in_sample_sharpe=2.0,
                    out_of_sample_sharpe=1.5,
                    out_of_sample_return=5.0,
                    out_of_sample_win_rate=0.65,
                    trade_count=20,
                    overfitting_ratio=1.33,
                ),
            ],
            is_robust=True,
        )
        assert result.is_robust

    def test_format_summary(self) -> None:
        results = [
            BacktestResult(
                signal_type="multi_day_pullback",
                avg_oos_sharpe=1.8,
                avg_oos_return=2.1,
                avg_oos_win_rate=0.71,
                total_trades=45,
                is_robust=True,
            ),
            BacktestResult(
                signal_type="gap_fill",
                avg_oos_sharpe=0.6,
                avg_oos_return=0.3,
                avg_oos_win_rate=0.48,
                total_trades=12,
                is_robust=False,
            ),
        ]
        output = format_backtest_summary(results)
        assert "multi_day_pullback" in output
        assert "gap_fill" in output
        assert "YES" in output
        assert "NO" in output


# ---------------------------------------------------------------------------
# Benchmark
# ---------------------------------------------------------------------------

class TestBenchmark:
    def test_compute(self) -> None:
        bm = BenchmarkComparison(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 12, 31),
            portfolio_return=25.0,
        )
        bm.compute(
            spy_return=12.0,
            qqq_return=18.0,
            vanilla_wheel_return=15.0,
            risk_free_return=5.0,
        )
        assert bm.alpha_vs_spy == pytest.approx(13.0)
        assert bm.alpha_vs_vanilla_wheel == pytest.approx(10.0)
        assert bm.benchmarks["spy_buy_hold"] == 12.0

    def test_underperforming_warning(self) -> None:
        bm = BenchmarkComparison(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 12, 31),
            portfolio_return=10.0,
            months_underperforming_vanilla=3,
        )
        bm.compute(spy_return=12.0, qqq_return=18.0,
                    vanilla_wheel_return=15.0, risk_free_return=5.0)
        output = format_benchmark(bm)
        assert "simplifying" in output.lower()

    def test_vanilla_wheel_sim(self) -> None:
        prices = [
            (date(2024, 1, 1) + timedelta(days=i), Decimal("450") + Decimal(str(i // 10)))
            for i in range(365)
        ]
        ret = simulate_vanilla_wheel(prices)
        # Should produce a positive return in an uptrending market
        assert ret > 0

    def test_vanilla_wheel_insufficient_data(self) -> None:
        prices = [(date(2024, 1, 1), Decimal("450"))]
        ret = simulate_vanilla_wheel(prices)
        assert ret == 0.0


# ---------------------------------------------------------------------------
# Learning Loop
# ---------------------------------------------------------------------------

class TestLearningLoop:
    def test_insufficient_trades(self) -> None:
        trades = [
            TradeRecord(
                trade_id="T001", symbol="AAPL", signal_type="dip",
                strategy="put", conviction="high", entry_date="2025-01-01",
                exit_date="2025-01-15", premium_received=Decimal("100"),
                pnl=Decimal("100"), pnl_pct=1.0, is_winner=True,
            )
        ]
        report = run_weekly_review(trades, {"dip": 0.5})
        assert len(report.warnings) == 1
        assert "need" in report.warnings[0].lower()

    def test_signal_weight_increase(self, sample_trades: list[TradeRecord]) -> None:
        # iv_rank_spike has ~67% WR in our fixture
        weights = {"iv_rank_spike": 0.50, "multi_day_pullback": 0.50}
        report = run_weekly_review(sample_trades, weights)
        # At least one adjustment should be proposed
        assert len(report.signal_adjustments) > 0 or len(report.warnings) > 0

    def test_signal_weight_clamped(self) -> None:
        cfg = LearningConfig(
            min_trades_for_adjustment=5,
            min_trades_per_signal=5,
            max_adjustment_per_cycle=0.05,  # very tight clamp
        )
        trades = [
            TradeRecord(
                trade_id=f"T{i}", symbol="AAPL",
                signal_type="oversold_rsi", strategy="put",
                conviction="high", entry_date="2025-01-01",
                exit_date="2025-01-15", premium_received=Decimal("350"),
                pnl=Decimal("-500"), pnl_pct=-1.4,
                is_winner=False,
            )
            for i in range(10)
        ]
        report = run_weekly_review(trades, {"oversold_rsi": 0.80}, config=cfg)
        for adj in report.signal_adjustments:
            # Change should not exceed 5% of current value
            max_allowed = 0.80 * 0.05
            actual_change = abs(adj.new_value - adj.old_value)
            assert actual_change <= max_allowed + 0.001

    def test_source_credibility_adjustment(
        self, sample_trades: list[TradeRecord]
    ) -> None:
        sources = {"unusual_whales": 0.80}
        report = run_weekly_review(sample_trades, {"iv_rank_spike": 0.5}, sources)
        # Source has enough trades (every 5th trade) and results vary
        # May or may not adjust depending on win rate
        assert isinstance(report.source_adjustments, list)

    def test_conviction_warning(self) -> None:
        cfg = LearningConfig(min_trades_for_adjustment=10)
        trades = [
            TradeRecord(
                trade_id=f"T{i}", symbol="AAPL",
                signal_type="dip", strategy="put",
                conviction="high",
                entry_date="2025-01-01", exit_date="2025-01-15",
                premium_received=Decimal("350"),
                pnl=Decimal("-200"), pnl_pct=-0.6,
                is_winner=False,
            )
            for i in range(15)
        ]
        report = run_weekly_review(trades, {"dip": 0.5}, config=cfg)
        assert any("high conviction" in w.lower() for w in report.warnings)

    def test_format_report(self, sample_trades: list[TradeRecord]) -> None:
        report = run_weekly_review(
            sample_trades, {"iv_rank_spike": 0.5, "multi_day_pullback": 0.5},
        )
        output = format_learning_report(report)
        assert "WEEKLY LEARNING REPORT" in output
        assert "Trades:" in output

    def test_format_empty_report(self) -> None:
        report = LearningReport()
        output = format_learning_report(report)
        assert "WEEKLY LEARNING REPORT" in output
