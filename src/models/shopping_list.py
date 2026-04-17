"""Shopping list models — external stock rating list integration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass
class ShoppingListEntry:
    """A stock from Parkev's rating list."""
    name: str
    ticker: str
    rating: str
    rating_tier: int          # 5=Top Stock, 4=Top 15, 3=Buy, 2=Borderline, 1=Hold, 0=Sell
    date_updated: date | None
    price_target_2026: tuple[Decimal, Decimal] | None  # (low, high)
    price_target_2027: tuple[Decimal, Decimal] | None
    stale: bool               # True if date_updated > 90 days ago


@dataclass
class BenchEntry:
    """A shopping list name screened for bench display."""
    ticker: str
    name: str
    rating: str
    current_price: Decimal
    price_target: str | None        # "500-550" display string
    upside_pct: float | None        # 0.12 = 12%
    iv_rank: float
    rsi: float
    next_earnings: date | None
    near_actionable: bool
    actionable_reason: str | None
    entry_price: float | None = None      # dynamic support level for entry
    entry_label: str | None = None        # e.g. "EMA 9", "SMA 20"
    target_low: float | None = None       # price target low
    target_high: float | None = None      # price target high
