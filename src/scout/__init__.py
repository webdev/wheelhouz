"""Scout module — social intelligence pipeline."""

from src.scout.aggregator import (
    RawMention,
    ScoutAnalysis,
    ScoutOpportunity,
    ScoutSource,
    aggregate_mentions,
    calculate_buzz_score,
    calculate_composite_score,
    format_scout_picks,
)
from src.scout.alerts import (
    ScoutAlertState,
    filter_for_alert,
    format_scout_alert,
)

__all__ = [
    "RawMention",
    "ScoutAnalysis",
    "ScoutOpportunity",
    "ScoutSource",
    "aggregate_mentions",
    "calculate_buzz_score",
    "calculate_composite_score",
    "format_scout_picks",
    "ScoutAlertState",
    "filter_for_alert",
    "format_scout_alert",
]
