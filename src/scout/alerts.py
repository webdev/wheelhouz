"""Scout intraday alerts — push high-urgency picks to Telegram."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from src.scout.aggregator import ScoutOpportunity


@dataclass
class ScoutAlertState:
    """Tracks alert throttling for scout picks."""
    alerts_today: int = 0
    max_alerts_per_day: int = 4
    min_composite_for_alert: float = 70.0
    max_per_run: int = 2
    last_reset: datetime = field(default_factory=datetime.utcnow)
    alerted_tickers_today: set[str] = field(default_factory=set)

    def reset_if_new_day(self) -> None:
        """Reset counters at midnight."""
        now = datetime.utcnow()
        if now.date() != self.last_reset.date():
            self.alerts_today = 0
            self.alerted_tickers_today = set()
            self.last_reset = now


def filter_for_alert(
    opportunities: list[ScoutOpportunity],
    state: ScoutAlertState,
) -> list[ScoutOpportunity]:
    """Filter scout opportunities to those worthy of an intraday push.

    Criteria: qualified, urgency="now", composite > 70, not already alerted.
    Returns max 2 per run.
    """
    state.reset_if_new_day()

    if state.alerts_today >= state.max_alerts_per_day:
        return []

    alertable: list[ScoutOpportunity] = []
    for opp in opportunities:
        if not opp.is_qualified:
            continue
        if opp.analysis.urgency != "now":
            continue
        if opp.composite_score < state.min_composite_for_alert:
            continue
        if opp.analysis.ticker in state.alerted_tickers_today:
            continue
        alertable.append(opp)

    # Take top N by composite score
    alertable.sort(key=lambda o: o.composite_score, reverse=True)
    remaining = state.max_alerts_per_day - state.alerts_today
    selected = alertable[:min(state.max_per_run, remaining)]

    # Update state
    for opp in selected:
        state.alerts_today += 1
        state.alerted_tickers_today.add(opp.analysis.ticker)

    return selected


def format_scout_alert(opp: ScoutOpportunity) -> str:
    """Format a scout pick as a Telegram intraday alert."""
    a = opp.analysis
    mention_count = len(opp.mentions)
    sources = {m.source for m in opp.mentions}

    return (
        f"SCOUT ALERT: {a.ticker}\n"
        f"Sentiment: {a.sentiment.upper()} | Buzz: {a.buzz_score}\n"
        f"Catalyst: {a.catalyst}\n"
        f"Wheel fit: {a.wheel_fit} ({a.wheel_fit_reasoning})\n"
        f"Strategy: {a.recommended_strategy}\n"
        f"Sources: {', '.join(sorted(sources))} ({mention_count} mentions)\n"
        f"Score: {opp.composite_score:.0f} | Urgency: {a.urgency}"
    )
