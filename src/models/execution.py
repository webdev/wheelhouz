"""Execution models: live-price gate, order management."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from src.models.signals import AlphaSignal


@dataclass
class LivePriceGate:
    """Conditions that must hold at execution time for a trade to be valid."""
    symbol: str
    trade_type: str
    strike: Decimal
    expiration: date

    # Original analysis context
    analysis_time: datetime
    analysis_price: Decimal
    analysis_premium: Decimal
    signals: list[AlphaSignal] = field(default_factory=list)
    conviction: str = "medium"

    # Gate conditions (ALL must pass)
    underlying_floor: Decimal = Decimal("0")
    underlying_ceiling: Decimal = Decimal("0")
    min_premium: Decimal = Decimal("0")
    min_iv_rank: float = 0.0
    max_abs_delta: float = 0.45

    # Safety limits
    disqualifying_events: list[str] = field(default_factory=list)
    max_age_hours: float = 8.0
    market_must_be_open: bool = True


@dataclass
class GateValidation:
    """Result of validating a LivePriceGate at execution time."""
    is_valid: bool
    reason: str
    checks_passed: list[str] = field(default_factory=list)
    checks_failed: list[str] = field(default_factory=list)
    live_price: Decimal = Decimal("0")
    live_premium: Decimal = Decimal("0")
    live_iv_rank: float = 0.0
    live_delta: float = 0.0
    validation_time: datetime = field(default_factory=datetime.utcnow)
