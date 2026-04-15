# tests/test_shopping_list.py
"""Tests for shopping list integration."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from src.models.shopping_list import BenchEntry, ShoppingListEntry


class TestShoppingListModels:
    def test_create_shopping_list_entry(self) -> None:
        entry = ShoppingListEntry(
            name="Alphabet",
            ticker="GOOG",
            rating="Buy",
            rating_tier=3,
            date_updated=date(2026, 3, 15),
            price_target_2026=(Decimal("200"), Decimal("220")),
            price_target_2027=(Decimal("250"), Decimal("280")),
            stale=False,
        )
        assert entry.ticker == "GOOG"
        assert entry.rating_tier == 3
        assert entry.price_target_2026 == (Decimal("200"), Decimal("220"))
        assert entry.stale is False

    def test_stale_entry(self) -> None:
        entry = ShoppingListEntry(
            name="Old Corp",
            ticker="OLD",
            rating="Hold/ Market Perform",
            rating_tier=1,
            date_updated=date(2025, 12, 1),
            price_target_2026=None,
            price_target_2027=None,
            stale=True,
        )
        assert entry.stale is True

    def test_create_bench_entry(self) -> None:
        entry = BenchEntry(
            ticker="HIMS",
            name="Hims & Hers Health",
            rating="Buy",
            current_price=Decimal("42.00"),
            price_target="45-55",
            upside_pct=0.15,
            iv_rank=72.0,
            rsi=28.0,
            next_earnings=date(2026, 5, 5),
            near_actionable=True,
            actionable_reason="IV rich + oversold",
        )
        assert entry.near_actionable is True
        assert entry.current_price == Decimal("42.00")
