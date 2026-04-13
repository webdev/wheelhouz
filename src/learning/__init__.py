"""Learning module — weekly self-tuning loop and performance attribution."""

from src.learning.attribution import (
    PerformanceAttribution,
    compute_attribution,
    format_attribution,
)
from src.learning.loop import (
    Adjustment,
    LearningConfig,
    LearningReport,
    TradeRecord,
    format_learning_report,
    run_weekly_review,
)

__all__ = [
    "PerformanceAttribution",
    "compute_attribution",
    "format_attribution",
    "Adjustment",
    "LearningConfig",
    "LearningReport",
    "TradeRecord",
    "format_learning_report",
    "run_weekly_review",
]
