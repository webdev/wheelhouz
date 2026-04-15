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


import pytest
from datetime import datetime
from unittest.mock import AsyncMock, patch, MagicMock
from src.data.shopping_list import (
    _parse_rating_tier,
    _parse_price_target,
    _parse_date,
    _parse_csv_rows,
    _MANUAL_OVERRIDES,
    resolve_ticker,
)


class TestCSVParsing:
    def test_rating_tier_mapping(self) -> None:
        assert _parse_rating_tier("Top Stock to Buy") == 5
        assert _parse_rating_tier("Top 15 Stock") == 4
        assert _parse_rating_tier("Buy") == 3
        assert _parse_rating_tier("Borderline Buy") == 2
        assert _parse_rating_tier("Hold/ Market Perform") == 1
        assert _parse_rating_tier("Sell") == 0
        assert _parse_rating_tier("Unknown Rating") == 1

    def test_price_target_range(self) -> None:
        result = _parse_price_target("500-550")
        assert result == (Decimal("500"), Decimal("550"))

    def test_price_target_with_commas(self) -> None:
        result = _parse_price_target("1,150-1,250")
        assert result == (Decimal("1150"), Decimal("1250"))

    def test_price_target_single_value(self) -> None:
        result = _parse_price_target("300")
        assert result == (Decimal("300"), Decimal("300"))

    def test_price_target_non_numeric(self) -> None:
        assert _parse_price_target("2-2.2 billion market cap") is None
        assert _parse_price_target("") is None
        assert _parse_price_target("N/A") is None

    def test_price_target_decimal_values(self) -> None:
        result = _parse_price_target("42.50-55.00")
        assert result == (Decimal("42.50"), Decimal("55.00"))

    def test_parse_date_formats(self) -> None:
        assert _parse_date("3/15/2026") == date(2026, 3, 15)
        assert _parse_date("12/1/2025") == date(2025, 12, 1)
        assert _parse_date("3/15/26") == date(2026, 3, 15)
        assert _parse_date("2026-03-15") == date(2026, 3, 15)
        assert _parse_date("") is None
        assert _parse_date("not-a-date") is None

    def test_manual_overrides_exist(self) -> None:
        assert _MANUAL_OVERRIDES["Alphabet"] == "GOOG"
        assert _MANUAL_OVERRIDES["Meta Platforms"] == "META"
        assert _MANUAL_OVERRIDES["Taiwan Semi"] == "TSM"

    def test_parse_csv_rows(self, monkeypatch) -> None:
        from src.data import shopping_list as sl_mod
        monkeypatch.setattr(sl_mod, "resolve_ticker", lambda name: {
            "Alphabet": "GOOG", "Bad Corp": "BAD",
        }.get(name.strip()))

        rows = [
            ["Alphabet", "Buy", "3/15/2026", "200-220", "4/1/2026", "250-280"],
            ["Bad Corp", "Sell", "", "", "", ""],
        ]
        entries = sl_mod._parse_csv_rows(rows, today=date(2026, 4, 15))
        assert len(entries) == 2
        assert entries[0].name == "Alphabet"
        assert entries[0].ticker == "GOOG"
        assert entries[0].rating_tier == 3
        assert entries[0].price_target_2026 == (Decimal("200"), Decimal("220"))
        assert entries[0].stale is False
        assert entries[1].rating == "Sell"
        assert entries[1].rating_tier == 0

    def test_stale_detection(self, monkeypatch) -> None:
        from src.data import shopping_list as sl_mod
        monkeypatch.setattr(sl_mod, "resolve_ticker", lambda name: "GOOG")

        rows = [
            ["Alphabet", "Buy", "12/1/2025", "200-220", "", ""],
        ]
        entries = sl_mod._parse_csv_rows(rows, today=date(2026, 4, 15))
        assert entries[0].stale is True


import asyncio


class TestFetchShoppingList:
    def test_fetch_from_cache(self, tmp_path, monkeypatch) -> None:
        """When cache is fresh, fetch_shopping_list reads from cache."""
        from src.data import shopping_list as sl_mod

        monkeypatch.setattr(sl_mod, "resolve_ticker", lambda name: {
            "Alphabet": "GOOG",
        }.get(name.strip()))

        cache_csv = (
            "Name,Rating,Date Updated,2026 Price Target,As of Date,2027 Price Target\n"
            "Alphabet,Buy,3/15/2026,200-220,,250-280\n"
        )
        cache_file = tmp_path / ".shopping_list_cache.csv"
        cache_file.write_text(cache_csv)
        ts_file = tmp_path / ".shopping_list_fetched"
        from datetime import timezone as _tz
        ts_file.write_text(datetime.now(_tz.utc).isoformat())

        monkeypatch.setattr(sl_mod, "_CACHE_FILE", cache_file)
        monkeypatch.setattr(sl_mod, "_TIMESTAMP_FILE", ts_file)
        monkeypatch.setattr(
            sl_mod, "load_trading_params",
            lambda: {"shopping_list": {"url": "http://fake", "cache_ttl_hours": 24}},
        )

        entries = asyncio.run(sl_mod.fetch_shopping_list())
        assert len(entries) >= 1
        assert entries[0].ticker == "GOOG"

    def test_fetch_returns_empty_on_no_data(self, tmp_path, monkeypatch) -> None:
        """When no cache and fetch fails, returns empty list."""
        from src.data import shopping_list as sl_mod

        monkeypatch.setattr(sl_mod, "_CACHE_FILE", tmp_path / "missing.csv")
        monkeypatch.setattr(sl_mod, "_TIMESTAMP_FILE", tmp_path / "missing_ts")
        monkeypatch.setattr(
            sl_mod, "load_trading_params",
            lambda: {"shopping_list": {"url": "http://will-fail", "cache_ttl_hours": 24}},
        )

        entries = asyncio.run(sl_mod.fetch_shopping_list())
        assert entries == []


class TestBenchBuilder:
    def test_bench_excludes_watchlist(self) -> None:
        from src.analysis.bench import _rank_and_filter

        entries = [
            ShoppingListEntry(
                name="In Watchlist", ticker="WATCH", rating="Buy", rating_tier=3,
                date_updated=date(2026, 4, 1),
                price_target_2026=(Decimal("100"), Decimal("120")),
                price_target_2027=None, stale=False,
            ),
            ShoppingListEntry(
                name="Not In Watchlist", ticker="FREE", rating="Buy", rating_tier=3,
                date_updated=date(2026, 4, 1),
                price_target_2026=(Decimal("50"), Decimal("60")),
                price_target_2027=None, stale=False,
            ),
        ]
        result = _rank_and_filter(entries, watchlist={"WATCH"}, scanner_symbols=set())
        tickers = [e.ticker for e in result]
        assert "WATCH" not in tickers
        assert "FREE" in tickers

    def test_bench_excludes_sell_rated(self) -> None:
        from src.analysis.bench import _rank_and_filter

        entries = [
            ShoppingListEntry(
                name="Sell Corp", ticker="SELL", rating="Sell", rating_tier=0,
                date_updated=date(2026, 4, 1),
                price_target_2026=None, price_target_2027=None, stale=False,
            ),
        ]
        result = _rank_and_filter(entries, watchlist=set(), scanner_symbols=set())
        assert len(result) == 0

    def test_near_actionable_iv_rich(self) -> None:
        from src.analysis.bench import _check_near_actionable

        actionable, reason = _check_near_actionable(
            iv_rank=72.0, rsi=45.0, next_earnings=None,
        )
        assert actionable is True
        assert "IV rank" in reason or "premium rich" in reason.lower()

    def test_near_actionable_oversold(self) -> None:
        from src.analysis.bench import _check_near_actionable

        actionable, reason = _check_near_actionable(
            iv_rank=30.0, rsi=28.0, next_earnings=None,
        )
        assert actionable is True
        assert "RSI" in reason or "oversold" in reason.lower()

    def test_near_actionable_earnings_blackout(self) -> None:
        from src.analysis.bench import _check_near_actionable

        actionable, reason = _check_near_actionable(
            iv_rank=30.0, rsi=50.0, next_earnings=date(2026, 4, 20),
            today=date(2026, 4, 15),
        )
        assert actionable is True
        assert "BLACKOUT" in reason

    def test_not_near_actionable(self) -> None:
        from src.analysis.bench import _check_near_actionable

        actionable, reason = _check_near_actionable(
            iv_rank=30.0, rsi=50.0, next_earnings=None,
        )
        assert actionable is False
        assert reason is None


from tests.fixtures import make_sized_opportunity


class TestConvictionModifier:
    def _make_entry(self, rating: str, tier: int, stale: bool = False) -> ShoppingListEntry:
        return ShoppingListEntry(
            name="Test Corp",
            ticker="TEST",
            rating=rating,
            rating_tier=tier,
            date_updated=date(2026, 4, 1),
            price_target_2026=(Decimal("100"), Decimal("120")),
            price_target_2027=None,
            stale=stale,
        )

    def test_top_stock_upgrades_by_2(self) -> None:
        from src.main import _apply_shopping_list_adjustment
        sized = make_sized_opportunity(conviction="low", symbol="TEST")
        entry = self._make_entry("Top Stock to Buy", 5)
        result, label = _apply_shopping_list_adjustment(sized, {"TEST": entry})
        assert result.conviction == "high"
        assert "Upgraded" in label
        assert "Parkev" in label

    def test_top_15_upgrades_by_1(self) -> None:
        from src.main import _apply_shopping_list_adjustment
        sized = make_sized_opportunity(conviction="medium", symbol="TEST")
        entry = self._make_entry("Top 15 Stock", 4)
        result, label = _apply_shopping_list_adjustment(sized, {"TEST": entry})
        assert result.conviction == "high"
        assert "Top 15" in label

    def test_buy_no_change(self) -> None:
        from src.main import _apply_shopping_list_adjustment
        sized = make_sized_opportunity(conviction="medium", symbol="TEST")
        entry = self._make_entry("Buy", 3)
        result, label = _apply_shopping_list_adjustment(sized, {"TEST": entry})
        assert result.conviction == "medium"
        assert label is None

    def test_hold_downgrades_by_1(self) -> None:
        from src.main import _apply_shopping_list_adjustment
        sized = make_sized_opportunity(conviction="high", symbol="TEST")
        entry = self._make_entry("Hold/ Market Perform", 1)
        result, label = _apply_shopping_list_adjustment(sized, {"TEST": entry})
        assert result.conviction == "medium"
        assert "Downgraded" in label

    def test_sell_downgrades_with_warning(self) -> None:
        from src.main import _apply_shopping_list_adjustment
        sized = make_sized_opportunity(conviction="medium", symbol="TEST")
        entry = self._make_entry("Sell", 0)
        result, label = _apply_shopping_list_adjustment(sized, {"TEST": entry})
        assert result.conviction == "low"
        assert "Sell-rated" in label

    def test_stale_entry_no_adjustment(self) -> None:
        from src.main import _apply_shopping_list_adjustment
        sized = make_sized_opportunity(conviction="low", symbol="TEST")
        entry = self._make_entry("Top Stock to Buy", 5, stale=True)
        result, label = _apply_shopping_list_adjustment(sized, {"TEST": entry})
        assert result.conviction == "low"  # unchanged
        assert label is None

    def test_not_in_list_no_change(self) -> None:
        from src.main import _apply_shopping_list_adjustment
        sized = make_sized_opportunity(conviction="medium", symbol="UNKNOWN")
        result, label = _apply_shopping_list_adjustment(sized, {})
        assert result.conviction == "medium"
        assert label is None

    def test_high_stays_high_on_upgrade(self) -> None:
        from src.main import _apply_shopping_list_adjustment
        sized = make_sized_opportunity(conviction="high", symbol="TEST")
        entry = self._make_entry("Top Stock to Buy", 5)
        result, label = _apply_shopping_list_adjustment(sized, {"TEST": entry})
        assert result.conviction == "high"  # already max
        assert label is None  # no change = no label

    def test_low_stays_low_on_downgrade_sell(self) -> None:
        from src.main import _apply_shopping_list_adjustment
        sized = make_sized_opportunity(conviction="low", symbol="TEST")
        entry = self._make_entry("Sell", 0)
        result, label = _apply_shopping_list_adjustment(sized, {"TEST": entry})
        # Sell on low → still low (can't go to skip via shopping list)
        assert result.conviction == "low"
        assert label is None  # no actual change


class TestScannerIntegration:
    def test_scanner_pick_has_shopping_list_fields(self) -> None:
        from src.main import ScannerPick
        pick = ScannerPick(
            symbol="LLY",
            price=710.0,
            iv_rank=65.0,
            rsi=32.0,
            put_contract=None,
            score=8.5,
            reasons=["IV rank 65", "RSI 32"],
            collateral_per_contract=67000.0,
            ann_yield=0.15,
            shopping_list_rating="Buy",
            price_target="1,150-1,250",
        )
        assert pick.shopping_list_rating == "Buy"
        assert pick.price_target == "1,150-1,250"

    def test_scanner_pick_defaults_none(self) -> None:
        from src.main import ScannerPick
        pick = ScannerPick(
            symbol="XYZ",
            price=50.0,
            iv_rank=40.0,
            rsi=45.0,
            put_contract=None,
            score=5.0,
            reasons=[],
            collateral_per_contract=5000.0,
            ann_yield=0.10,
        )
        assert pick.shopping_list_rating is None
        assert pick.price_target is None
