"""Regime detection — classifies market environment from VIX and SPY.

Runs every 60 seconds during market hours (independent of 5x daily schedule).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class RegimeThresholds:
    """VIX-based regime boundaries."""
    attack_vix_max: float = 18.0
    hold_vix_max: float = 25.0
    defend_vix_max: float = 35.0
    # Above defend_vix_max = CRISIS

    # SPY drop thresholds
    elevated_spy_drop: float = -0.02
    severe_spy_drop: float = -0.03
    crisis_spy_drop: float = -0.05
    extreme_spy_drop: float = -0.08

    # Deployment targets per regime
    attack_deployed: float = 0.90
    hold_deployed: float = 0.70
    defend_deployed: float = 0.40
    crisis_deployed: float = 0.10


@dataclass
class RegimeState:
    """Current regime classification with supporting data."""
    regime: str  # "attack", "hold", "defend", "crisis"
    vix: float
    spy_change_pct: float
    severity: str  # "normal", "elevated", "severe", "crisis", "extreme"
    target_deployed: float
    timestamp: datetime
    changed_from: str | None = None  # previous regime if changed


def classify_regime(
    vix: float,
    spy_change_pct: float,
    thresholds: RegimeThresholds | None = None,
) -> RegimeState:
    """Classify current market regime from VIX level and SPY movement.

    VIX determines the base regime. SPY drop can escalate it.
    """
    t = thresholds or RegimeThresholds()
    now = datetime.utcnow()

    # Base regime from VIX
    if vix < t.attack_vix_max:
        regime = "attack"
        target = t.attack_deployed
    elif vix < t.hold_vix_max:
        regime = "hold"
        target = t.hold_deployed
    elif vix < t.defend_vix_max:
        regime = "defend"
        target = t.defend_deployed
    else:
        regime = "crisis"
        target = t.crisis_deployed

    # Severity from SPY drop (can escalate regime)
    if spy_change_pct <= t.extreme_spy_drop:
        severity = "extreme"
        regime = "crisis"
        target = t.crisis_deployed
    elif spy_change_pct <= t.crisis_spy_drop:
        severity = "crisis"
        if regime != "crisis":
            regime = "crisis"
            target = t.crisis_deployed
    elif spy_change_pct <= t.severe_spy_drop:
        severity = "severe"
        if regime == "attack":
            regime = "defend"
            target = t.defend_deployed
    elif spy_change_pct <= t.elevated_spy_drop:
        severity = "elevated"
    else:
        severity = "normal"

    return RegimeState(
        regime=regime,
        vix=vix,
        spy_change_pct=spy_change_pct,
        severity=severity,
        target_deployed=target,
        timestamp=now,
    )


def detect_regime_change(
    previous: RegimeState | None,
    current: RegimeState,
) -> bool:
    """Check if regime has changed (requires immediate action)."""
    if previous is None:
        return False
    if previous.regime != current.regime:
        current.changed_from = previous.regime
        return True
    return False


def format_regime_alert(state: RegimeState) -> str:
    """Format a regime change alert for Telegram push."""
    direction = ""
    if state.changed_from:
        order = ["attack", "hold", "defend", "crisis"]
        old_idx = order.index(state.changed_from) if state.changed_from in order else 0
        new_idx = order.index(state.regime) if state.regime in order else 0
        direction = "ESCALATION" if new_idx > old_idx else "DE-ESCALATION"

    return (
        f"REGIME CHANGE: {state.regime.upper()}"
        f"{f' ({direction})' if direction else ''}\n"
        f"VIX: {state.vix:.1f} | SPY: {state.spy_change_pct:+.1f}%\n"
        f"Severity: {state.severity}\n"
        f"Target deployed: {state.target_deployed:.0%}"
    )
