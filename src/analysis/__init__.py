"""Analysis engine — signals, strikes, sizing, scanning, ranking.

Public interface for the analysis layer.
"""

from src.analysis.opportunities import find_and_rank_opportunities
from src.analysis.scanner import scan_position
from src.analysis.signals import detect_all_signals
from src.analysis.sizing import size_position
from src.analysis.strikes import find_smart_strikes

__all__ = [
    "detect_all_signals",
    "find_smart_strikes",
    "size_position",
    "scan_position",
    "find_and_rank_opportunities",
]
