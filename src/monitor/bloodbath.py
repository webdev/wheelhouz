"""Bloodbath protocol — crisis management for market crashes.

Three crisis modes:
1. BROAD MARKET CRASH: SPY -5%+ in a day, VIX >35
2. SECTOR REPRICING: tech/software names diverge from broad market
3. EMPLOYER-SPECIFIC CRISIS: ADBE -20%+ in a week

Phases: instant detection → crisis protection → sector analysis →
employer override → recovery playbook.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


# ---------------------------------------------------------------------------
# Phase 0: Instant crisis detection
# ---------------------------------------------------------------------------

@dataclass
class CrisisLevel:
    """Detected crisis severity with supporting data."""
    level: str  # "none", "elevated", "severe", "crisis", "extreme"
    vix: float
    spy_drop_pct: float
    triggers: list[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.utcnow)


CRISIS_THRESHOLDS = {
    "elevated": {"vix": 25.0, "spy_drop": -0.02},
    "severe": {"vix": 30.0, "spy_drop": -0.03},
    "crisis": {"vix": 35.0, "spy_drop": -0.05},
    "extreme": {"vix": 45.0, "spy_drop": -0.08},
}


def detect_crisis_level(vix: float, spy_drop_pct: float) -> CrisisLevel:
    """Classify current crisis level from VIX and SPY movement."""
    triggers: list[str] = []
    level = "none"

    for severity in ("extreme", "crisis", "severe", "elevated"):
        thresholds = CRISIS_THRESHOLDS[severity]
        if vix >= thresholds["vix"]:
            triggers.append(f"VIX {vix:.1f} >= {thresholds['vix']}")
            if level == "none":
                level = severity
        if spy_drop_pct <= thresholds["spy_drop"]:
            triggers.append(f"SPY {spy_drop_pct:.1%} <= {thresholds['spy_drop']:.0%}")
            if level == "none" or _severity_rank(severity) > _severity_rank(level):
                level = severity

    return CrisisLevel(level=level, vix=vix, spy_drop_pct=spy_drop_pct, triggers=triggers)


def _severity_rank(level: str) -> int:
    return {"none": 0, "elevated": 1, "severe": 2, "crisis": 3, "extreme": 4}.get(level, 0)


# ---------------------------------------------------------------------------
# Phase 1: Crisis protection — order filling + margin stress
# ---------------------------------------------------------------------------

@dataclass
class CrisisAction:
    """An action to take during a crisis."""
    action: str  # "close_weeklies", "cancel_pending", "preemptive_close", "block_new"
    ticker: str | None = None
    reason: str = ""
    urgency: str = "immediate"


def determine_crisis_actions(
    crisis: CrisisLevel,
    weekly_positions: list[dict[str, object]],
    pending_orders: int,
    margin_utilization: float,
) -> list[CrisisAction]:
    """Determine protective actions based on crisis level."""
    actions: list[CrisisAction] = []

    if crisis.level in ("crisis", "extreme"):
        # Close ALL weeklies
        for pos in weekly_positions:
            actions.append(CrisisAction(
                action="close_weeklies",
                ticker=str(pos.get("symbol", "")),
                reason=f"Crisis protocol: close DTE<10 positions",
            ))

        # Cancel pending orders
        if pending_orders > 0:
            actions.append(CrisisAction(
                action="cancel_pending",
                reason=f"Cancel {pending_orders} pending orders",
            ))

        # Block new trades
        actions.append(CrisisAction(
            action="block_new",
            reason="Crisis: no new positions until regime clears",
        ))

    if crisis.level in ("severe", "crisis", "extreme"):
        # Margin stress check
        if margin_utilization > 0.60:
            actions.append(CrisisAction(
                action="preemptive_close",
                reason=(
                    f"Margin at {margin_utilization:.0%} — "
                    f"preemptive close to avoid margin call"
                ),
            ))

    return actions


def calculate_crisis_fill_price(
    mid_price: Decimal,
    bid: Decimal,
    ask: Decimal,
    seconds_waiting: int,
) -> Decimal:
    """Calculate aggressive fill price during crisis (wide spreads).

    Strategy: try mid for 30s → walk toward bid $0.25 every 15s → market after 2min.
    """
    if seconds_waiting <= 30:
        return mid_price
    elif seconds_waiting <= 120:
        # Walk toward bid by $0.25 per 15-second interval
        steps = (seconds_waiting - 30) // 15
        walk = Decimal("0.25") * steps
        return max(bid, mid_price - walk)
    else:
        # Market order — use bid
        return bid


def project_margin_stress(
    current_margin_pct: float,
    portfolio_beta: float,
    additional_spy_drop_pct: float,
) -> tuple[float, bool]:
    """Project margin utilization if SPY drops further.

    Returns (projected_margin_pct, would_trigger_call).
    Margin call threshold: 85%.
    """
    # Rough projection: portfolio loss ≈ beta × SPY drop
    projected_loss_pct = abs(additional_spy_drop_pct) * portfolio_beta
    projected_margin = current_margin_pct + projected_loss_pct * 0.5
    return (projected_margin, projected_margin > 0.85)


# ---------------------------------------------------------------------------
# Phase 2: Sector repricing detection
# ---------------------------------------------------------------------------

@dataclass
class SectorRepricingAnalysis:
    """Analysis of whether a crash is sector-specific or broad."""
    is_sector_specific: bool
    divergence_pct: float  # spread between winners and losers
    narrative: str
    winners: list[str] = field(default_factory=list)
    losers: list[str] = field(default_factory=list)
    loser_premium_multiplier: float = 1.0  # how much richer premiums are


def detect_sector_repricing(
    position_changes: dict[str, float],
    spy_change: float,
) -> SectorRepricingAnalysis:
    """Detect if bloodbath is sector-specific vs broad market.

    Divergence > 8% between winners and losers = sector repricing.
    """
    if not position_changes:
        return SectorRepricingAnalysis(
            is_sector_specific=False, divergence_pct=0.0, narrative="No positions",
        )

    winners = [s for s, c in position_changes.items() if c > spy_change + 0.02]
    losers = [s for s, c in position_changes.items() if c < spy_change - 0.02]
    all_changes = list(position_changes.values())

    best = max(all_changes) if all_changes else 0.0
    worst = min(all_changes) if all_changes else 0.0
    divergence = best - worst

    is_sector = divergence > 0.08 and len(winners) > 0 and len(losers) > 0

    # Premium on losers during repricing is typically 3-5x normal
    premium_mult = 1.0
    if is_sector and losers:
        avg_loser_drop = sum(position_changes[s] for s in losers) / len(losers)
        premium_mult = min(5.0, max(1.0, 1.0 + abs(avg_loser_drop) * 20))

    narrative = ""
    if is_sector:
        narrative = (
            f"Sector repricing: {len(winners)} winners vs {len(losers)} losers. "
            f"Divergence {divergence:.0%}. Loser premiums ~{premium_mult:.1f}x normal."
        )
    elif divergence > 0.03:
        narrative = f"Moderate divergence ({divergence:.0%}) but below repricing threshold."
    else:
        narrative = f"Broad-based move. Low divergence ({divergence:.0%})."

    return SectorRepricingAnalysis(
        is_sector_specific=is_sector,
        divergence_pct=divergence,
        narrative=narrative,
        winners=winners,
        losers=losers,
        loser_premium_multiplier=premium_mult,
    )


# ---------------------------------------------------------------------------
# Phase 3: Employer crisis (ADBE)
# ---------------------------------------------------------------------------

@dataclass
class EmployerCrisisAlert:
    """ADBE-specific crisis detection and action."""
    triggered: bool
    adbe_change_5d_pct: float
    adbe_concentration_pct: float
    action: str  # "none", "sell_50pct", "sell_to_target"
    reason: str


def check_employer_crisis(
    adbe_change_5d_pct: float,
    adbe_nlv_pct: float,
    target_concentration: float = 0.15,
) -> EmployerCrisisAlert:
    """Check for ADBE-specific crisis.

    Trigger: ADBE -20% in 5 trading days.
    Tax efficiency NEVER overrides employer-correlated risk.
    """
    if adbe_change_5d_pct <= -0.20:
        return EmployerCrisisAlert(
            triggered=True,
            adbe_change_5d_pct=adbe_change_5d_pct,
            adbe_concentration_pct=adbe_nlv_pct,
            action="sell_50pct",
            reason=(
                f"ADBE down {adbe_change_5d_pct:.0%} in 5 days. "
                f"Emergency: sell 50% above {target_concentration:.0%} target. "
                f"Tax efficiency overridden by employer risk."
            ),
        )
    elif adbe_nlv_pct > target_concentration:
        return EmployerCrisisAlert(
            triggered=False,
            adbe_change_5d_pct=adbe_change_5d_pct,
            adbe_concentration_pct=adbe_nlv_pct,
            action="sell_to_target",
            reason=(
                f"ADBE at {adbe_nlv_pct:.0%} NLV (target {target_concentration:.0%}). "
                f"Continue quarterly sell plan."
            ),
        )
    else:
        return EmployerCrisisAlert(
            triggered=False,
            adbe_change_5d_pct=adbe_change_5d_pct,
            adbe_concentration_pct=adbe_nlv_pct,
            action="none",
            reason=f"ADBE at {adbe_nlv_pct:.0%} NLV — within target.",
        )


# ---------------------------------------------------------------------------
# Phase 4: Recovery playbook
# ---------------------------------------------------------------------------

STABILIZATION_SIGNALS = [
    "vix_peak",              # VIX made lower high
    "volume_climax",         # Record volume day followed by lower volume
    "breadth_improvement",   # Advance/decline ratio improving
    "sector_leadership",     # 3+ sectors turning green
    "credit_stabilize",      # High-yield spreads stopped widening
    "vix_term_contango",     # Front < back month VIX (normal structure)
]


@dataclass
class RecoveryAssessment:
    """Assessment of whether recovery conditions are met."""
    signals_present: list[str]
    signals_needed: int = 3
    ready_for_recovery: bool = False
    deployment_pct: float = 0.0  # how much dry powder to deploy
    strategy: str = ""


def assess_recovery(
    signals_present: list[str],
    dry_powder_pct: float,
    signals_needed: int = 3,
) -> RecoveryAssessment:
    """Assess if conditions support recovery deployment.

    Need 3+ stabilization signals. Deploy 25-50% dry powder.
    Monthly puts only, no weeklies. Scale in over 3-5 days.
    """
    valid_signals = [s for s in signals_present if s in STABILIZATION_SIGNALS]
    ready = len(valid_signals) >= signals_needed

    deployment = 0.0
    strategy = ""
    if ready:
        if len(valid_signals) >= 5:
            deployment = min(0.50, dry_powder_pct * 0.50)
            strategy = "Aggressive recovery: deploy 50% powder over 3 days"
        elif len(valid_signals) >= 4:
            deployment = min(0.35, dry_powder_pct * 0.35)
            strategy = "Moderate recovery: deploy 35% powder over 4 days"
        else:
            deployment = min(0.25, dry_powder_pct * 0.25)
            strategy = "Cautious recovery: deploy 25% powder over 5 days"
        strategy += ". Monthly puts only. Keep 25-50% reserve for second leg down."

    return RecoveryAssessment(
        signals_present=valid_signals,
        signals_needed=signals_needed,
        ready_for_recovery=ready,
        deployment_pct=deployment,
        strategy=strategy,
    )


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_crisis_alert(
    crisis: CrisisLevel,
    actions: list[CrisisAction],
) -> str:
    """Format crisis alert for Telegram push."""
    lines = [f"BLOODBATH PROTOCOL — {crisis.level.upper()}"]
    for t in crisis.triggers:
        lines.append(f"  ! {t}")

    if actions:
        lines.append("\nIMMEDIATE ACTIONS:")
        for a in actions:
            ticker_str = f" {a.ticker}" if a.ticker else ""
            lines.append(f"  [{a.action.upper()}]{ticker_str}: {a.reason}")

    return "\n".join(lines)
