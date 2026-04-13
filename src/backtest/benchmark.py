"""Benchmark comparison — portfolio vs SPY, QQQ, vanilla wheel, risk-free."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass
class BenchmarkComparison:
    """Compare portfolio performance against standard benchmarks."""
    period_start: date
    period_end: date
    portfolio_return: float

    benchmarks: dict[str, float] = field(default_factory=dict)
    alpha_vs_spy: float = 0.0
    alpha_vs_vanilla_wheel: float = 0.0

    # Kill switch: consecutive months underperforming vanilla wheel
    months_underperforming_vanilla: int = 0

    def compute(
        self,
        spy_return: float,
        qqq_return: float,
        vanilla_wheel_return: float,
        risk_free_return: float,
    ) -> None:
        """Compute all benchmark comparisons."""
        self.benchmarks = {
            "spy_buy_hold": spy_return,
            "qqq_buy_hold": qqq_return,
            "spy_vanilla_wheel": vanilla_wheel_return,
            "risk_free": risk_free_return,
        }
        self.alpha_vs_spy = self.portfolio_return - spy_return
        self.alpha_vs_vanilla_wheel = self.portfolio_return - vanilla_wheel_return


def simulate_vanilla_wheel(
    prices: list[tuple[date, Decimal]],
    delta: float = 0.25,
    dte: int = 30,
) -> float:
    """Simulate a vanilla SPY wheel (30-DTE, 0.25 delta, no signals).

    Returns annualized return percentage.
    """
    if len(prices) < dte + 1:
        return 0.0

    total_premium = Decimal("0")
    capital = prices[0][1] * 100  # 1 contract worth of SPY

    i = 0
    while i + dte < len(prices):
        entry_price = prices[i][1]
        exit_price = prices[i + dte][1]

        # Premium ≈ 1.5% of underlying for 0.25 delta 30-DTE
        premium = entry_price * Decimal("0.015")
        strike = entry_price * Decimal("0.97")  # ~3% OTM

        if exit_price >= strike:
            # OTM at expiry — keep premium
            total_premium += premium
        else:
            # ITM — assigned, lose the intrinsic value
            loss = strike - exit_price
            total_premium += premium - loss

        i += dte

    if capital == 0:
        return 0.0

    total_return = float(total_premium / capital)
    days = (prices[-1][0] - prices[0][0]).days or 1
    annualized = total_return * (365 / days)
    return annualized * 100


def format_benchmark(comparison: BenchmarkComparison) -> str:
    """Format benchmark comparison for briefing."""
    lines = [
        "BENCHMARK COMPARISON",
        f"Your portfolio:      {comparison.portfolio_return:+.1f}%",
    ]
    for name, ret in comparison.benchmarks.items():
        label = name.replace("_", " ").title()
        lines.append(f"{label:<21}{ret:+.1f}%")

    lines.append("")
    alpha_spy = comparison.alpha_vs_spy
    alpha_vanilla = comparison.alpha_vs_vanilla_wheel
    lines.append(
        f"Alpha vs SPY:        {alpha_spy:+.1f}%  "
        f"{'OK' if alpha_spy > 0 else 'UNDERPERFORMING'}"
    )
    lines.append(
        f"Alpha vs vanilla:    {alpha_vanilla:+.1f}%  "
        f"{'OK' if alpha_vanilla > 0 else 'UNDERPERFORMING'}"
    )

    if comparison.months_underperforming_vanilla >= 3:
        lines.append(
            "\nWARNING: 3+ months underperforming vanilla wheel. "
            "Consider simplifying."
        )

    return "\n".join(lines)
