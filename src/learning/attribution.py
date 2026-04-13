"""Performance attribution — tracks returns by engine, strategy, signal, conviction.

Answers: what's working, what's not, and where is alpha coming from?
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class PerformanceAttribution:
    """Attribution breakdown for a period."""
    period: str  # "mtd", "qtd", "ytd"

    # By engine
    engine1_return: float = 0.0
    engine2_return: float = 0.0
    engine3_return: float = 0.0
    blended_return: float = 0.0

    # By strategy (Engine 2 breakdown)
    strategy_returns: dict[str, float] = field(default_factory=dict)

    # By signal type
    signal_performance: dict[str, dict[str, float]] = field(default_factory=dict)
    # {signal_type: {"trades": N, "win_rate": 0.XX, "avg_return": X.X}}

    # By conviction level
    high_conviction_return: float = 0.0
    medium_conviction_return: float = 0.0
    low_conviction_return: float = 0.0

    # Scout performance
    scout_pick_return: float = 0.0
    scout_win_rate: float = 0.0
    scout_vs_watchlist: float = 0.0

    # Actual realized Sharpe
    realized_sharpe: float = 0.0


def compute_attribution(
    trades: list[dict[str, object]],
    period: str = "ytd",
) -> PerformanceAttribution:
    """Compute performance attribution from closed trades.

    Each trade dict must have: strategy, signal_type, conviction,
    engine, pnl_pct, is_winner, is_scout_pick.
    """
    attr = PerformanceAttribution(period=period)

    if not trades:
        return attr

    # By strategy
    by_strategy: dict[str, list[float]] = {}
    by_signal: dict[str, list[dict[str, object]]] = {}
    by_conviction: dict[str, list[float]] = {}
    by_engine: dict[str, list[float]] = {}
    scout_returns: list[float] = []
    non_scout_returns: list[float] = []

    for t in trades:
        pnl = float(str(t.get("pnl_pct", 0)))
        strategy = str(t.get("strategy", "unknown"))
        signal = str(t.get("signal_type", "unknown"))
        conviction = str(t.get("conviction", "unknown"))
        engine = str(t.get("engine", "engine2"))
        is_scout = bool(t.get("is_scout_pick", False))

        by_strategy.setdefault(strategy, []).append(pnl)
        by_signal.setdefault(signal, []).append(t)
        by_conviction.setdefault(conviction, []).append(pnl)
        by_engine.setdefault(engine, []).append(pnl)

        if is_scout:
            scout_returns.append(pnl)
        else:
            non_scout_returns.append(pnl)

    # Strategy returns
    for strat, returns in by_strategy.items():
        attr.strategy_returns[strat] = sum(returns) / len(returns) if returns else 0.0

    # Signal performance
    for signal, signal_trades in by_signal.items():
        count = len(signal_trades)
        winners = sum(bool(t.get("is_winner")) for t in signal_trades)
        avg_ret = sum(float(str(t.get("pnl_pct", 0))) for t in signal_trades) / count
        attr.signal_performance[signal] = {
            "trades": float(count),
            "win_rate": winners / count if count else 0.0,
            "avg_return": avg_ret,
        }

    # Conviction returns
    for level in ("high", "medium", "low"):
        returns = by_conviction.get(level, [])
        avg = sum(returns) / len(returns) if returns else 0.0
        if level == "high":
            attr.high_conviction_return = avg
        elif level == "medium":
            attr.medium_conviction_return = avg
        else:
            attr.low_conviction_return = avg

    # Engine returns
    for eng in ("engine1", "engine2", "engine3"):
        returns = by_engine.get(eng, [])
        avg = sum(returns) / len(returns) if returns else 0.0
        if eng == "engine1":
            attr.engine1_return = avg
        elif eng == "engine2":
            attr.engine2_return = avg
        else:
            attr.engine3_return = avg

    # Blended
    all_returns = [float(str(t.get("pnl_pct", 0))) for t in trades]
    attr.blended_return = sum(all_returns) / len(all_returns) if all_returns else 0.0

    # Scout vs non-scout
    if scout_returns:
        attr.scout_pick_return = sum(scout_returns) / len(scout_returns)
        attr.scout_win_rate = sum(1 for r in scout_returns if r > 0) / len(scout_returns)
    if non_scout_returns:
        attr.scout_vs_watchlist = attr.scout_pick_return - (
            sum(non_scout_returns) / len(non_scout_returns)
        )

    # Sharpe approximation
    if len(all_returns) > 1:
        import statistics
        std = statistics.stdev(all_returns) or 0.001
        attr.realized_sharpe = (attr.blended_return / std) * (len(all_returns) ** 0.5)

    return attr


def format_attribution(attr: PerformanceAttribution) -> str:
    """Format attribution for weekly review."""
    lines = [f"PERFORMANCE ATTRIBUTION ({attr.period.upper()})"]

    lines.append(f"\nBY ENGINE:")
    lines.append(f"  E1 (Core):   {attr.engine1_return:+.2f}%")
    lines.append(f"  E2 (Wheel):  {attr.engine2_return:+.2f}%")
    lines.append(f"  E3 (Powder): {attr.engine3_return:+.2f}%")
    lines.append(f"  Blended:     {attr.blended_return:+.2f}%")

    if attr.strategy_returns:
        lines.append(f"\nBY STRATEGY:")
        for strat, ret in sorted(attr.strategy_returns.items(),
                                  key=lambda x: x[1], reverse=True):
            lines.append(f"  {strat:<20} {ret:+.2f}%")

    lines.append(f"\nBY CONVICTION:")
    lines.append(f"  HIGH:   {attr.high_conviction_return:+.2f}%")
    lines.append(f"  MEDIUM: {attr.medium_conviction_return:+.2f}%")
    lines.append(f"  LOW:    {attr.low_conviction_return:+.2f}%")

    if attr.scout_pick_return != 0:
        lines.append(f"\nSCOUT PICKS:")
        lines.append(f"  Return:       {attr.scout_pick_return:+.2f}%")
        lines.append(f"  Win rate:     {attr.scout_win_rate:.0%}")
        lines.append(f"  vs Watchlist: {attr.scout_vs_watchlist:+.2f}%")

    lines.append(f"\nRealized Sharpe: {attr.realized_sharpe:.2f}")

    return "\n".join(lines)
