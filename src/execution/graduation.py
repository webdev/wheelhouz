"""Go-live checklist and auto-execution graduation.

Go-live criteria (all must be met):
- 60+ paper trades completed
- Win rate >= 55%
- HIGH conviction WR >= 65%
- Max drawdown < 12%
- Loss stops triggered 3+ times (proves they work)

Auto-execution graduation (months after go-live):
- M1-2: Manual approval on everything
- M3: Auto-close winners at 50% profit
- M4: Auto-execute HIGH conviction trades
- M5+: Auto-execute HIGH + MEDIUM
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class GoLiveCriteria:
    """Thresholds that must be met before going live."""
    min_trades: int = 60
    min_win_rate: float = 0.55
    min_high_conviction_wr: float = 0.65
    max_drawdown_pct: float = 0.12
    min_loss_stops_triggered: int = 3


@dataclass
class GoLiveStatus:
    """Current status of go-live readiness."""
    total_trades: int = 0
    win_rate: float = 0.0
    high_conviction_wr: float = 0.0
    max_drawdown_pct: float = 0.0
    loss_stops_triggered: int = 0

    # Individual checks
    trades_ok: bool = False
    win_rate_ok: bool = False
    high_wr_ok: bool = False
    drawdown_ok: bool = False
    loss_stops_ok: bool = False

    # Overall
    ready: bool = False
    blockers: list[str] = field(default_factory=list)


def evaluate_go_live(
    total_trades: int,
    win_rate: float,
    high_conviction_wr: float,
    max_drawdown_pct: float,
    loss_stops_triggered: int,
    criteria: GoLiveCriteria | None = None,
) -> GoLiveStatus:
    """Evaluate whether the system is ready to go live."""
    c = criteria or GoLiveCriteria()
    status = GoLiveStatus(
        total_trades=total_trades,
        win_rate=win_rate,
        high_conviction_wr=high_conviction_wr,
        max_drawdown_pct=max_drawdown_pct,
        loss_stops_triggered=loss_stops_triggered,
    )

    status.trades_ok = total_trades >= c.min_trades
    if not status.trades_ok:
        status.blockers.append(
            f"Need {c.min_trades - total_trades} more trades "
            f"({total_trades}/{c.min_trades})"
        )

    status.win_rate_ok = win_rate >= c.min_win_rate
    if not status.win_rate_ok:
        status.blockers.append(
            f"Win rate {win_rate:.0%} below {c.min_win_rate:.0%}"
        )

    status.high_wr_ok = high_conviction_wr >= c.min_high_conviction_wr
    if not status.high_wr_ok:
        status.blockers.append(
            f"HIGH conviction WR {high_conviction_wr:.0%} "
            f"below {c.min_high_conviction_wr:.0%}"
        )

    status.drawdown_ok = max_drawdown_pct <= c.max_drawdown_pct
    if not status.drawdown_ok:
        status.blockers.append(
            f"Max DD {max_drawdown_pct:.1%} exceeds {c.max_drawdown_pct:.0%}"
        )

    status.loss_stops_ok = loss_stops_triggered >= c.min_loss_stops_triggered
    if not status.loss_stops_ok:
        status.blockers.append(
            f"Loss stops triggered {loss_stops_triggered}x "
            f"(need {c.min_loss_stops_triggered}+)"
        )

    status.ready = all([
        status.trades_ok,
        status.win_rate_ok,
        status.high_wr_ok,
        status.drawdown_ok,
        status.loss_stops_ok,
    ])

    return status


# ---------------------------------------------------------------------------
# Auto-execution graduation
# ---------------------------------------------------------------------------

@dataclass
class ExecutionLevel:
    """Current auto-execution permission level."""
    level: int  # 1-5
    name: str
    description: str
    auto_close_winners: bool = False
    auto_execute_high: bool = False
    auto_execute_medium: bool = False
    requires_manual_approval: bool = True


EXECUTION_LEVELS = {
    1: ExecutionLevel(1, "Manual", "All trades require manual approval",
                      requires_manual_approval=True),
    2: ExecutionLevel(2, "Manual+", "Manual approval, learning system behavior",
                      requires_manual_approval=True),
    3: ExecutionLevel(3, "Auto-Close", "Auto-close winners at 50% profit",
                      auto_close_winners=True, requires_manual_approval=True),
    4: ExecutionLevel(4, "Auto-HIGH", "Auto-execute HIGH conviction trades",
                      auto_close_winners=True, auto_execute_high=True,
                      requires_manual_approval=False),
    5: ExecutionLevel(5, "Auto-HIGH+MED", "Auto-execute HIGH + MEDIUM",
                      auto_close_winners=True, auto_execute_high=True,
                      auto_execute_medium=True, requires_manual_approval=False),
}


def determine_execution_level(months_live: int) -> ExecutionLevel:
    """Determine execution level based on months since go-live."""
    if months_live <= 1:
        return EXECUTION_LEVELS[1]
    elif months_live == 2:
        return EXECUTION_LEVELS[2]
    elif months_live == 3:
        return EXECUTION_LEVELS[3]
    elif months_live == 4:
        return EXECUTION_LEVELS[4]
    else:
        return EXECUTION_LEVELS[5]


def should_auto_execute(
    conviction: str,
    level: ExecutionLevel,
) -> tuple[bool, str]:
    """Check if a trade should be auto-executed at the current level.

    Returns (auto_execute, reason).
    """
    if conviction == "high" and level.auto_execute_high:
        return (True, f"AUTO: HIGH conviction at Level {level.level} ({level.name})")
    elif conviction == "medium" and level.auto_execute_medium:
        return (True, f"AUTO: MEDIUM conviction at Level {level.level} ({level.name})")
    else:
        return (False, f"MANUAL: {conviction} conviction at Level {level.level}")


def format_go_live_status(status: GoLiveStatus) -> str:
    """Format go-live status for dashboard display."""
    checks = [
        ("Trades", status.trades_ok, f"{status.total_trades}/60"),
        ("Win Rate", status.win_rate_ok, f"{status.win_rate:.0%}"),
        ("HIGH WR", status.high_wr_ok, f"{status.high_conviction_wr:.0%}"),
        ("Max DD", status.drawdown_ok, f"{status.max_drawdown_pct:.1%}"),
        ("Loss Stops", status.loss_stops_ok, f"{status.loss_stops_triggered}x"),
    ]

    lines = ["GO-LIVE CHECKLIST"]
    for name, passed, value in checks:
        mark = "OK" if passed else "--"
        lines.append(f"  [{mark}] {name}: {value}")

    if status.ready:
        lines.append("\nREADY FOR GO-LIVE")
    else:
        lines.append(f"\nBLOCKERS ({len(status.blockers)}):")
        for b in status.blockers:
            lines.append(f"  - {b}")

    return "\n".join(lines)
