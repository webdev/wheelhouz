"""Walk-forward backtesting engine.

Train on N days, test on M days, step forward S days.
Only out-of-sample performance counts — prevents overfitting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal


@dataclass
class WalkForwardConfig:
    """Walk-forward backtest configuration."""
    train_window: int = 252       # 1 year in-sample
    test_window: int = 126        # 6 months out-of-sample
    step_size: int = 63           # step 3 months
    min_trades_per_window: int = 10
    initial_capital: Decimal = Decimal("100000")
    slippage_per_contract: Decimal = Decimal("0.03")
    commission_per_contract: Decimal = Decimal("0.65")


@dataclass
class WindowResult:
    """Result from a single train/test window."""
    train_start: date
    train_end: date
    test_start: date
    test_end: date
    in_sample_sharpe: float
    out_of_sample_sharpe: float
    out_of_sample_return: float
    out_of_sample_win_rate: float
    trade_count: int
    overfitting_ratio: float  # IS sharpe / OOS sharpe; > 2.0 = likely overfitted


@dataclass
class BacktestResult:
    """Aggregated walk-forward backtest result."""
    signal_type: str
    windows: list[WindowResult] = field(default_factory=list)
    avg_oos_sharpe: float = 0.0
    avg_oos_return: float = 0.0
    avg_oos_win_rate: float = 0.0
    avg_overfitting_ratio: float = 0.0
    total_trades: int = 0
    is_robust: bool = False  # True if all OOS sharpe > 0.8


@dataclass
class SignalBacktestSummary:
    """Summary row for a single signal across all windows."""
    signal_type: str
    win_rate: float
    avg_return: float
    sharpe: float
    trade_count: int
    optimal_threshold: float
    is_viable: bool  # OOS sharpe >= 1.0


def run_walk_forward(
    signal_type: str,
    daily_prices: dict[str, list[tuple[date, Decimal]]],
    config: WalkForwardConfig | None = None,
) -> BacktestResult:
    """Run walk-forward backtest for a single signal type.

    daily_prices: {symbol: [(date, close_price), ...]} sorted ascending.
    Returns aggregated OOS performance.
    """
    cfg = config or WalkForwardConfig()
    result = BacktestResult(signal_type=signal_type)

    # Flatten all dates to find the overall range
    all_dates: list[date] = sorted({
        d for prices in daily_prices.values() for d, _ in prices
    })
    if len(all_dates) < cfg.train_window + cfg.test_window:
        return result

    # Walk forward
    start_idx = 0
    while start_idx + cfg.train_window + cfg.test_window <= len(all_dates):
        train_start = all_dates[start_idx]
        train_end = all_dates[start_idx + cfg.train_window - 1]
        test_start = all_dates[start_idx + cfg.train_window]
        test_end_idx = min(
            start_idx + cfg.train_window + cfg.test_window - 1,
            len(all_dates) - 1,
        )
        test_end = all_dates[test_end_idx]

        # Simulate: optimize on train, test on OOS
        train_result = _simulate_window(
            signal_type, daily_prices, train_start, train_end, cfg,
        )
        test_result = _simulate_window(
            signal_type, daily_prices, test_start, test_end, cfg,
        )

        oos_sharpe = test_result["sharpe"]
        is_sharpe = train_result["sharpe"]
        overfit = is_sharpe / max(oos_sharpe, 0.01) if oos_sharpe != 0 else 99.0

        window = WindowResult(
            train_start=train_start,
            train_end=train_end,
            test_start=test_start,
            test_end=test_end,
            in_sample_sharpe=is_sharpe,
            out_of_sample_sharpe=oos_sharpe,
            out_of_sample_return=test_result["return"],
            out_of_sample_win_rate=test_result["win_rate"],
            trade_count=int(test_result["trades"]),
            overfitting_ratio=overfit,
        )
        result.windows.append(window)
        result.total_trades += int(test_result["trades"])

        start_idx += cfg.step_size

    if result.windows:
        result.avg_oos_sharpe = sum(
            w.out_of_sample_sharpe for w in result.windows
        ) / len(result.windows)
        result.avg_oos_return = sum(
            w.out_of_sample_return for w in result.windows
        ) / len(result.windows)
        result.avg_oos_win_rate = sum(
            w.out_of_sample_win_rate for w in result.windows
        ) / len(result.windows)
        result.avg_overfitting_ratio = sum(
            w.overfitting_ratio for w in result.windows
        ) / len(result.windows)
        result.is_robust = all(
            w.out_of_sample_sharpe > 0.8 for w in result.windows
        )

    return result


def _simulate_window(
    signal_type: str,
    daily_prices: dict[str, list[tuple[date, Decimal]]],
    start: date,
    end: date,
    config: WalkForwardConfig,
) -> dict[str, float]:
    """Simulate signal performance over a date window.

    Returns dict with keys: sharpe, return, win_rate, trades.
    This is a simplified simulation — real implementation would
    reconstruct option chains via Black-Scholes.
    """
    returns: list[float] = []

    for symbol, prices in daily_prices.items():
        window_prices = [
            (d, p) for d, p in prices if start <= d <= end
        ]
        if len(window_prices) < 20:
            continue

        # Simple signal simulation: look for dip entries
        for i in range(5, len(window_prices)):
            d, price = window_prices[i]
            prev_price = window_prices[i - 5][1]

            triggered = _check_signal_trigger(
                signal_type, price, prev_price, window_prices[:i],
            )
            if not triggered:
                continue

            # Simulate put sale: premium ≈ 2% of underlying
            premium_pct = 0.02
            # Outcome: check if underlying stays above 95% of entry
            exit_idx = min(i + 30, len(window_prices) - 1)
            exit_price = window_prices[exit_idx][1]
            strike_pct = 0.95

            if float(exit_price / price) >= strike_pct:
                trade_return = premium_pct
            else:
                loss = float((price * Decimal(str(strike_pct)) - exit_price) / price)
                trade_return = premium_pct - loss

            trade_return -= float(
                config.slippage_per_contract + config.commission_per_contract
            ) / float(price) / 100
            returns.append(trade_return)

    if not returns:
        return {"sharpe": 0.0, "return": 0.0, "win_rate": 0.0, "trades": 0}

    avg_return = sum(returns) / len(returns)
    win_rate = sum(1 for r in returns if r > 0) / len(returns)

    # Sharpe approximation (annualized)
    if len(returns) > 1:
        import statistics
        std = statistics.stdev(returns) or 0.001
        sharpe = (avg_return / std) * (252 ** 0.5) / 10  # scaled
    else:
        sharpe = 0.0

    return {
        "sharpe": sharpe,
        "return": avg_return * 100,
        "win_rate": win_rate,
        "trades": len(returns),
    }


def _check_signal_trigger(
    signal_type: str,
    current_price: Decimal,
    price_5d_ago: Decimal,
    history: list[tuple[date, Decimal]],
) -> bool:
    """Check if a signal would fire based on simplified criteria."""
    if price_5d_ago == 0:
        return False
    change_5d = float((current_price - price_5d_ago) / price_5d_ago)

    if signal_type == "multi_day_pullback":
        return change_5d < -0.05
    elif signal_type == "intraday_dip":
        return change_5d < -0.02
    elif signal_type == "oversold_rsi":
        # Simplified: 5+ day pullback > 8%
        return change_5d < -0.08
    elif signal_type == "support_bounce":
        # Price within 2% of 50-day low
        if len(history) < 50:
            return False
        low_50 = min(p for _, p in history[-50:])
        return float((current_price - low_50) / low_50) < 0.02
    elif signal_type == "iv_rank_spike":
        # Proxy: large move implies IV spike
        return abs(change_5d) > 0.06
    elif signal_type == "macro_fear_spike":
        return change_5d < -0.04
    elif signal_type == "volume_climax":
        return change_5d < -0.07
    else:
        # Default: 3%+ pullback
        return change_5d < -0.03


def format_backtest_summary(results: list[BacktestResult]) -> str:
    """Format multiple backtest results into a comparison table."""
    lines = [
        "WALK-FORWARD BACKTEST RESULTS",
        f"{'Signal':<25} {'Win%':>6} {'Return':>8} {'Sharpe':>7} "
        f"{'Trades':>7} {'Robust':>7}",
        "-" * 65,
    ]
    for r in sorted(results, key=lambda x: x.avg_oos_sharpe, reverse=True):
        lines.append(
            f"{r.signal_type:<25} {r.avg_oos_win_rate:>5.0%} "
            f"{r.avg_oos_return:>+7.1f}% {r.avg_oos_sharpe:>7.2f} "
            f"{r.total_trades:>7} {'YES' if r.is_robust else 'NO':>7}"
        )
    return "\n".join(lines)
