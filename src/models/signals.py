"""Alpha signal models for opportunity detection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from src.models.enums import SignalType


@dataclass
class AlphaSignal:
    """A detected trading signal with strength and direction."""
    symbol: str
    signal_type: SignalType
    strength: float          # 0-100, used for position sizing
    direction: str           # "sell_put", "sell_call", "sit"
    reasoning: str
    expires: datetime        # signal decays — when does this stop being valid?
