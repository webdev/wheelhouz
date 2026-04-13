"""Learning loop — weekly self-tuning of signal weights and strategy allocations.

Runs Saturday morning. Adjusts signal weights, thresholds, conviction sizing,
and scout credibility based on actual trade performance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class LearningConfig:
    """Tuning parameters for the weekly learning loop."""
    min_trades_for_adjustment: int = 20
    max_adjustment_per_cycle: float = 0.15  # 15% max change
    min_trades_per_signal: int = 5
    min_trades_per_source: int = 3

    # Thresholds for weight changes
    increase_sharpe_min: float = 2.0
    increase_win_rate_min: float = 0.70
    decrease_sharpe_max: float = 0.8
    decrease_win_rate_max: float = 0.50

    # Weight floors/ceilings
    min_signal_weight: float = 0.10
    max_signal_weight: float = 1.00
    min_source_credibility: float = 0.10
    max_source_credibility: float = 1.00


@dataclass
class TradeRecord:
    """A closed trade for performance analysis."""
    trade_id: str
    symbol: str
    signal_type: str
    strategy: str
    conviction: str
    entry_date: str
    exit_date: str
    premium_received: Decimal
    pnl: Decimal
    pnl_pct: float
    is_winner: bool
    scout_source: str | None = None


@dataclass
class SignalPerformance:
    """Performance stats for a single signal type."""
    signal_type: str
    trade_count: int
    win_rate: float
    avg_return: float
    sharpe: float
    current_weight: float
    proposed_weight: float
    change_direction: str = ""  # "up", "down", "unchanged"
    change_reason: str = ""


@dataclass
class Adjustment:
    """A single parameter adjustment proposed by the learning loop."""
    param_name: str
    old_value: float
    new_value: float
    reason: str
    clamped: bool = False  # True if was limited by max_adjustment_per_cycle


@dataclass
class LearningReport:
    """Output of the weekly learning loop."""
    signal_adjustments: list[Adjustment] = field(default_factory=list)
    strategy_adjustments: list[Adjustment] = field(default_factory=list)
    source_adjustments: list[Adjustment] = field(default_factory=list)
    overall_stats: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def run_weekly_review(
    trades: list[TradeRecord],
    current_signal_weights: dict[str, float],
    current_source_credibility: dict[str, float] | None = None,
    config: LearningConfig | None = None,
) -> LearningReport:
    """Run the full weekly learning loop.

    Analyzes closed trades and proposes parameter adjustments.
    """
    cfg = config or LearningConfig()
    report = LearningReport()

    if len(trades) < cfg.min_trades_for_adjustment:
        report.warnings.append(
            f"Only {len(trades)} trades — need {cfg.min_trades_for_adjustment} "
            f"before adjusting. Skipping tuning."
        )
        return report

    # Overall stats
    winners = [t for t in trades if t.is_winner]
    report.overall_stats = {
        "total_trades": len(trades),
        "win_rate": len(winners) / len(trades),
        "avg_return": sum(t.pnl_pct for t in trades) / len(trades),
    }

    # 1. Retune signal weights
    report.signal_adjustments = _retune_signal_weights(
        trades, current_signal_weights, cfg,
    )

    # 2. Retune scout source credibility
    if current_source_credibility:
        report.source_adjustments = _retune_source_credibility(
            trades, current_source_credibility, cfg,
        )

    # 3. Check conviction sizing
    _check_conviction_performance(trades, report, cfg)

    return report


def _retune_signal_weights(
    trades: list[TradeRecord],
    current_weights: dict[str, float],
    cfg: LearningConfig,
) -> list[Adjustment]:
    """Adjust signal weights based on trade performance."""
    adjustments: list[Adjustment] = []

    # Group by signal type
    by_signal: dict[str, list[TradeRecord]] = {}
    for t in trades:
        by_signal.setdefault(t.signal_type, []).append(t)

    for signal_type, signal_trades in by_signal.items():
        if len(signal_trades) < cfg.min_trades_per_signal:
            continue

        win_rate = sum(1 for t in signal_trades if t.is_winner) / len(signal_trades)
        avg_ret = sum(t.pnl_pct for t in signal_trades) / len(signal_trades)
        current = current_weights.get(signal_type, 0.50)

        # Calculate Sharpe approximation
        returns = [t.pnl_pct for t in signal_trades]
        if len(returns) > 1:
            import statistics
            std = statistics.stdev(returns) or 0.001
            sharpe = (avg_ret / std) * (len(returns) ** 0.5)
        else:
            sharpe = 0.0

        new_weight = current
        reason = ""

        if sharpe > cfg.increase_sharpe_min and win_rate > cfg.increase_win_rate_min:
            new_weight = min(current * 1.10, current + 0.05)
            reason = (
                f"Strong: Sharpe {sharpe:.1f}, WR {win_rate:.0%} "
                f"on {len(signal_trades)} trades"
            )
        elif sharpe < cfg.decrease_sharpe_max or win_rate < cfg.decrease_win_rate_max:
            new_weight = max(current * 0.85, cfg.min_signal_weight)
            reason = (
                f"Weak: Sharpe {sharpe:.1f}, WR {win_rate:.0%} "
                f"on {len(signal_trades)} trades"
            )
        else:
            continue

        # Clamp to max adjustment
        new_weight = _clamp_adjustment(current, new_weight, cfg)
        new_weight = max(cfg.min_signal_weight, min(cfg.max_signal_weight, new_weight))

        if abs(new_weight - current) > 0.001:
            adjustments.append(Adjustment(
                param_name=f"signal_weight.{signal_type}",
                old_value=current,
                new_value=round(new_weight, 3),
                reason=reason,
                clamped=abs(new_weight - current) >= cfg.max_adjustment_per_cycle * current,
            ))

    return adjustments


def _retune_source_credibility(
    trades: list[TradeRecord],
    current_cred: dict[str, float],
    cfg: LearningConfig,
) -> list[Adjustment]:
    """Adjust scout source credibility from trade outcomes."""
    adjustments: list[Adjustment] = []

    by_source: dict[str, list[TradeRecord]] = {}
    for t in trades:
        if t.scout_source:
            by_source.setdefault(t.scout_source, []).append(t)

    for source, source_trades in by_source.items():
        if len(source_trades) < cfg.min_trades_per_source:
            continue

        win_rate = sum(1 for t in source_trades if t.is_winner) / len(source_trades)
        current = current_cred.get(source, 0.50)

        if win_rate > 0.65:
            new_cred = min(current + 0.05, cfg.max_source_credibility)
            reason = f"WR {win_rate:.0%} on {len(source_trades)} trades"
        elif win_rate < 0.40:
            new_cred = max(current - 0.10, cfg.min_source_credibility)
            reason = f"WR {win_rate:.0%} on {len(source_trades)} trades"
        else:
            continue

        if abs(new_cred - current) > 0.001:
            adjustments.append(Adjustment(
                param_name=f"source_credibility.{source}",
                old_value=current,
                new_value=round(new_cred, 3),
                reason=reason,
            ))

    return adjustments


def _check_conviction_performance(
    trades: list[TradeRecord],
    report: LearningReport,
    cfg: LearningConfig,
) -> None:
    """Check if conviction levels are performing as expected."""
    by_conviction: dict[str, list[TradeRecord]] = {}
    for t in trades:
        by_conviction.setdefault(t.conviction, []).append(t)

    for level in ("high", "medium", "low"):
        level_trades = by_conviction.get(level, [])
        if len(level_trades) < 10:
            continue
        wr = sum(1 for t in level_trades if t.is_winner) / len(level_trades)
        if level == "high" and wr < 0.55:
            report.warnings.append(
                f"HIGH conviction WR only {wr:.0%} — review signal quality"
            )
        if level == "low" and wr < 0.45:
            report.warnings.append(
                f"LOW conviction WR {wr:.0%} — consider skipping LOW trades"
            )


def _clamp_adjustment(
    current: float, proposed: float, cfg: LearningConfig,
) -> float:
    """Clamp an adjustment to max_adjustment_per_cycle."""
    max_change = abs(current) * cfg.max_adjustment_per_cycle
    delta = proposed - current
    if abs(delta) > max_change:
        return current + (max_change if delta > 0 else -max_change)
    return proposed


def format_learning_report(report: LearningReport) -> str:
    """Format learning report for Telegram push."""
    lines = ["WEEKLY LEARNING REPORT"]

    if report.overall_stats:
        stats = report.overall_stats
        lines.append(
            f"Trades: {stats.get('total_trades', 0):.0f} | "
            f"WR: {stats.get('win_rate', 0):.0%} | "
            f"Avg: {stats.get('avg_return', 0):+.1f}%"
        )

    if report.signal_adjustments:
        lines.append("\nSIGNAL WEIGHT CHANGES:")
        for adj in report.signal_adjustments:
            arrow = "^" if adj.new_value > adj.old_value else "v"
            lines.append(
                f"  {arrow} {adj.param_name}: "
                f"{adj.old_value:.2f} -> {adj.new_value:.2f} "
                f"({adj.reason})"
            )

    if report.source_adjustments:
        lines.append("\nSOURCE CREDIBILITY CHANGES:")
        for adj in report.source_adjustments:
            arrow = "^" if adj.new_value > adj.old_value else "v"
            lines.append(
                f"  {arrow} {adj.param_name}: "
                f"{adj.old_value:.2f} -> {adj.new_value:.2f} "
                f"({adj.reason})"
            )

    if report.warnings:
        lines.append("\nWARNINGS:")
        for w in report.warnings:
            lines.append(f"  ! {w}")

    if not report.signal_adjustments and not report.warnings:
        lines.append("\nNo adjustments needed. All signals performing within range.")

    return "\n".join(lines)
