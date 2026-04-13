"""Backtesting module — walk-forward validation and benchmarking."""

from src.backtest.benchmark import (
    BenchmarkComparison,
    format_benchmark,
    simulate_vanilla_wheel,
)
from src.backtest.engine import (
    BacktestResult,
    SignalBacktestSummary,
    WalkForwardConfig,
    WindowResult,
    format_backtest_summary,
    run_walk_forward,
)

__all__ = [
    "BenchmarkComparison",
    "format_benchmark",
    "simulate_vanilla_wheel",
    "BacktestResult",
    "SignalBacktestSummary",
    "WalkForwardConfig",
    "WindowResult",
    "format_backtest_summary",
    "run_walk_forward",
]
