"""Pre-market sentinel — early morning futures check.

Runs at 6:00, 7:00, 7:30 AM ET on trading days.
Checks ES/NQ/VX futures against prior close for overnight risk.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class SentinelThresholds:
    """Emergency thresholds for pre-market checks."""
    spy_futures_drop: float = -0.02       # -2% overnight
    vix_futures_spike: float = 5.0        # +5 points overnight
    nasdaq_futures_drop: float = -0.03    # -3% overnight


@dataclass
class SentinelAlert:
    """Pre-market sentinel alert."""
    triggered: bool
    severity: str  # "normal", "elevated", "emergency"
    spy_futures_pct: float
    nasdaq_futures_pct: float
    vix_futures_change: float
    triggers: list[str]
    estimated_pnl: float | None = None
    margin_concern: bool = False
    weekly_options_exposed: bool = False
    timestamp: datetime | None = None


def check_premarket(
    spy_futures_pct: float,
    nasdaq_futures_pct: float,
    vix_futures_change: float,
    has_weekly_options: bool = False,
    margin_utilization: float = 0.0,
    thresholds: SentinelThresholds | None = None,
) -> SentinelAlert:
    """Run pre-market sentinel check.

    Returns SentinelAlert with triggered=True if any threshold breached.
    """
    t = thresholds or SentinelThresholds()
    triggers: list[str] = []

    if spy_futures_pct <= t.spy_futures_drop:
        triggers.append(
            f"SPY futures {spy_futures_pct:+.1%} (threshold: {t.spy_futures_drop:.0%})"
        )

    if nasdaq_futures_pct <= t.nasdaq_futures_drop:
        triggers.append(
            f"Nasdaq futures {nasdaq_futures_pct:+.1%} "
            f"(threshold: {t.nasdaq_futures_drop:.0%})"
        )

    if vix_futures_change >= t.vix_futures_spike:
        triggers.append(
            f"VIX futures +{vix_futures_change:.1f} "
            f"(threshold: +{t.vix_futures_spike:.0f})"
        )

    severity = "normal"
    if len(triggers) >= 2:
        severity = "emergency"
    elif len(triggers) == 1:
        severity = "elevated"

    margin_concern = margin_utilization > 0.60 and len(triggers) > 0
    weekly_exposed = has_weekly_options and len(triggers) > 0

    return SentinelAlert(
        triggered=len(triggers) > 0,
        severity=severity,
        spy_futures_pct=spy_futures_pct,
        nasdaq_futures_pct=nasdaq_futures_pct,
        vix_futures_change=vix_futures_change,
        triggers=triggers,
        margin_concern=margin_concern,
        weekly_options_exposed=weekly_exposed,
        timestamp=datetime.utcnow(),
    )


def format_sentinel_alert(alert: SentinelAlert) -> str:
    """Format sentinel alert for Telegram push."""
    if not alert.triggered:
        return (
            f"PRE-MARKET: All clear\n"
            f"ES: {alert.spy_futures_pct:+.1%} | "
            f"NQ: {alert.nasdaq_futures_pct:+.1%} | "
            f"VIX: {alert.vix_futures_change:+.1f}"
        )

    lines = [
        f"PRE-MARKET ALERT ({alert.severity.upper()})",
    ]
    for t in alert.triggers:
        lines.append(f"  ! {t}")

    if alert.margin_concern:
        lines.append("  !! MARGIN CONCERN — monitor closely at open")
    if alert.weekly_options_exposed:
        lines.append("  !! Weekly options exposed — consider closing at open")

    return "\n".join(lines)
