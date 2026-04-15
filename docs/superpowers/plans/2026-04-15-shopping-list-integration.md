# Shopping List Integration Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate Parkev's Google Sheets stock rating list as the primary scanner discovery universe, add a conviction modifier based on ratings, and display a BENCH section in the briefing for names approaching entry readiness.

**Architecture:** Three new components feed into the existing pipeline: (1) data layer fetches/caches/parses the CSV and resolves names→tickers, (2) conviction modifier adjusts trade conviction after TradingView adjustment, (3) bench builder screens top shopping list names with lightweight technicals. The scanner switches from Finviz-primary to shopping-list-primary with Finviz as backfill.

**Tech Stack:** Python 3.11+, httpx (async HTTP), yfinance (ticker resolution + batch quotes), csv stdlib, structlog, pytest

---

### Task 1: Shopping List Models

Create the shared dataclasses for the shopping list feature. These are used by every other task.

**Files:**
- Create: `src/models/shopping_list.py`
- Modify: `src/models/__init__.py`
- Modify: `src/models/analysis.py:41-56`
- Modify: `tests/fixtures/trades.py:41-62`
- Test: `tests/test_shopping_list.py`

- [ ] **Step 1: Write the failing test for ShoppingListEntry**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_shopping_list.py::TestShoppingListModels -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.models.shopping_list'`

- [ ] **Step 3: Create the models file**

```python
# src/models/shopping_list.py
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
```

- [ ] **Step 4: Add conviction_label to SizedOpportunity**

In `src/models/analysis.py`, add one field to the `SizedOpportunity` dataclass, after line 56 (`reasoning: str = ""`):

```python
    conviction_label: str | None = None
```

- [ ] **Step 5: Update fixtures to include conviction_label default**

In `tests/fixtures/trades.py`, inside the `make_sized_opportunity` defaults dict (after the `"reasoning"` key), add:

```python
        "conviction_label": None,
```

- [ ] **Step 6: Add exports to src/models/__init__.py**

Add import at line 10 (after the analysis import):

```python
from src.models.shopping_list import BenchEntry, ShoppingListEntry
```

Add to `__all__` list (after the `"RiskReport"` entry):

```python
    # Shopping list
    "ShoppingListEntry",
    "BenchEntry",
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/test_shopping_list.py::TestShoppingListModels -v`
Expected: 3 passed

- [ ] **Step 8: Run full test suite for regression**

Run: `pytest tests/ -v --tb=short`
Expected: All 81+ tests pass (conviction_label default of None is backwards-compatible)

- [ ] **Step 9: Commit**

```bash
git add src/models/shopping_list.py src/models/analysis.py src/models/__init__.py tests/test_shopping_list.py tests/fixtures/trades.py
git commit -m "feat: add shopping list models (ShoppingListEntry, BenchEntry, conviction_label)"
```

---

### Task 2: Config — Shopping List URL

Add the configurable URL to `trading_params.yaml`.

**Files:**
- Modify: `config/trading_params.yaml:103`

- [ ] **Step 1: Add shopping_list config section**

Append to the end of `config/trading_params.yaml`:

```yaml

shopping_list:
  url: "https://docs.google.com/spreadsheets/d/12Fs_d8Zr4sKnoCxb5EaEbe2FciXIGPVTFGM9iehZq3M/gviz/tq?tqx=out:csv"
  cache_ttl_hours: 24
```

- [ ] **Step 2: Verify config loads**

Run: `python -c "from src.config.loader import load_trading_params; p = load_trading_params(); print(p.get('shopping_list', {}).get('url', 'MISSING'))"`
Expected: Prints the Google Sheets URL

- [ ] **Step 3: Commit**

```bash
git add config/trading_params.yaml
git commit -m "config: add shopping_list.url to trading_params"
```

---

### Task 3: Data Layer — Fetch, Parse, Resolve

Build the async data layer that fetches the CSV, parses it, resolves names to tickers, and caches results.

**Files:**
- Create: `src/data/shopping_list.py`
- Modify: `src/data/__init__.py`
- Test: `tests/test_shopping_list.py` (append to existing)

- [ ] **Step 1: Write failing tests for CSV parsing**

Append to `tests/test_shopping_list.py`:

```python
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, patch, MagicMock
from src.data.shopping_list import (
    _parse_rating_tier,
    _parse_price_target,
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
        assert _parse_rating_tier("Unknown Rating") == 1  # default to Hold

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

    def test_manual_overrides_exist(self) -> None:
        assert _MANUAL_OVERRIDES["Alphabet"] == "GOOG"
        assert _MANUAL_OVERRIDES["Meta Platforms"] == "META"
        assert _MANUAL_OVERRIDES["Taiwan Semi"] == "TSM"

    def test_parse_csv_rows(self, monkeypatch) -> None:
        from src.data import shopping_list as sl_mod
        # Mock _resolve_ticker so we don't hit yfinance
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
        assert entries[0].ticker == "GOOG"  # from manual overrides
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
        assert entries[0].stale is True  # > 90 days old
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_shopping_list.py::TestCSVParsing -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement the data layer**

```python
# src/data/shopping_list.py
"""Shopping list data layer — fetch, cache, parse, resolve tickers."""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import structlog

from src.config.loader import load_trading_params
from src.models.shopping_list import ShoppingListEntry

log = structlog.get_logger()

_CONFIG_DIR = Path("config")
_CACHE_FILE = _CONFIG_DIR / ".shopping_list_cache.csv"
_TIMESTAMP_FILE = _CONFIG_DIR / ".shopping_list_fetched"
_TICKER_MAP_FILE = _CONFIG_DIR / ".ticker_map.json"

_RATING_TIERS: dict[str, int] = {
    "Top Stock to Buy": 5,
    "Top 15 Stock": 4,
    "Buy": 3,
    "Borderline Buy": 2,
    "Hold/ Market Perform": 1,
    "Sell": 0,
}

_MANUAL_OVERRIDES: dict[str, str] = {
    "Alphabet": "GOOG",
    "Meta Platforms": "META",
    "Taiwan Semi": "TSM",
    "British American Tob.": "BTI",
    "Eli Lilly": "LLY",
    "Pinduoduo": "PDD",
    "Mercadolibre": "MELI",
    "Booking Holdings": "BKNG",
    "The Trade Desk": "TTD",
    "Deer": "DE",
    "Carnival Cruise Line": "CCL",
    "Royal Caribbean Cruise": "RCL",
    "Procter & Gamble": "PG",
    "Keurig Dr Pepper": "KDP",
    "Unite Parsel Service": "UPS",
    "Lumen Tech": "LUMN",
    "Luminar Tech": "LAZR",
    "S&P Global": "SPGI",
    "Corning ": "GLW",
    "Exxon ": "XOM",
    "Cameco ": "CCJ",
    "PACCAR ": "PCAR",
}


def _parse_rating_tier(rating: str) -> int:
    """Map rating string to numeric tier. Defaults to 1 (Hold) for unknowns."""
    return _RATING_TIERS.get(rating.strip(), 1)


def _parse_price_target(raw: str) -> tuple[Decimal, Decimal] | None:
    """Parse price target string like '500-550' or '1,150-1,250'.

    Returns (low, high) as Decimal, or None if not parseable.
    """
    if not raw or not raw.strip():
        return None
    cleaned = raw.strip().replace(",", "")
    # Match patterns like "500-550", "42.50-55.00", or just "300"
    match = re.match(r"^(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)$", cleaned)
    if match:
        try:
            return (Decimal(match.group(1)), Decimal(match.group(2)))
        except InvalidOperation:
            return None
    # Single number
    match_single = re.match(r"^(\d+(?:\.\d+)?)$", cleaned)
    if match_single:
        try:
            val = Decimal(match_single.group(1))
            return (val, val)
        except InvalidOperation:
            return None
    return None


def _parse_date(raw: str) -> date | None:
    """Parse date string like '3/15/2026' or '12/1/2025'."""
    if not raw or not raw.strip():
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    return None


def resolve_ticker(name: str) -> str | None:
    """Resolve company name to ticker symbol.

    Checks manual overrides first, then persistent cache, then yfinance.
    Public API — exported via src/data/__init__.py.
    """
    # 1. Manual overrides (exact match, including trailing spaces)
    if name in _MANUAL_OVERRIDES:
        return _MANUAL_OVERRIDES[name]
    # Also try stripped version
    stripped = name.strip()
    if stripped in _MANUAL_OVERRIDES:
        return _MANUAL_OVERRIDES[stripped]

    # 2. Check persistent ticker map cache
    ticker_map = _load_ticker_map()
    if stripped in ticker_map:
        return ticker_map[stripped]

    # 3. Auto-resolve via yfinance
    try:
        import yfinance as yf
        search = yf.Search(stripped)
        if search.quotes:
            ticker = search.quotes[0].get("symbol")
            if ticker:
                _save_ticker_map(stripped, ticker)
                return ticker
    except Exception as e:
        log.warning("ticker_resolution_failed", name=stripped, error=str(e))

    return None


def _load_ticker_map() -> dict[str, str]:
    """Load the persistent name→ticker cache."""
    if _TICKER_MAP_FILE.exists():
        try:
            return json.loads(_TICKER_MAP_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_ticker_map(name: str, ticker: str) -> None:
    """Save a name→ticker mapping to the persistent cache."""
    ticker_map = _load_ticker_map()
    ticker_map[name] = ticker
    _TICKER_MAP_FILE.write_text(json.dumps(ticker_map, indent=2))


def _parse_csv_rows(
    rows: list[list[str]],
    today: date | None = None,
) -> list[ShoppingListEntry]:
    """Parse CSV data rows into ShoppingListEntry objects.

    Columns: 0=Name, 1=Rating, 2=Date Updated, 3=2026 Target, 5=2027 Target.
    """
    if today is None:
        today = date.today()
    stale_cutoff = today - timedelta(days=90)
    entries: list[ShoppingListEntry] = []

    for row in rows:
        if len(row) < 2:
            continue
        name = row[0].strip()
        rating = row[1].strip() if len(row) > 1 else ""
        if not name or not rating:
            continue
        # Skip header or metadata rows
        if name == "Name" or name.startswith("*"):
            continue

        ticker = resolve_ticker(name)
        if not ticker:
            log.debug("shopping_list_skip_no_ticker", name=name)
            continue

        date_updated = _parse_date(row[2]) if len(row) > 2 else None
        price_target_2026 = _parse_price_target(row[3]) if len(row) > 3 else None
        price_target_2027 = _parse_price_target(row[5]) if len(row) > 5 else None

        stale = date_updated is not None and date_updated < stale_cutoff

        entries.append(ShoppingListEntry(
            name=name,
            ticker=ticker,
            rating=rating,
            rating_tier=_parse_rating_tier(rating),
            date_updated=date_updated,
            price_target_2026=price_target_2026,
            price_target_2027=price_target_2027,
            stale=stale,
        ))

    return entries


def _cache_is_fresh(ttl_hours: int = 24) -> bool:
    """Check if the cache file exists and is within TTL."""
    if not _CACHE_FILE.exists() or not _TIMESTAMP_FILE.exists():
        return False
    try:
        ts = datetime.fromisoformat(_TIMESTAMP_FILE.read_text().strip())
        return datetime.now() - ts < timedelta(hours=ttl_hours)
    except (ValueError, OSError):
        return False


async def fetch_shopping_list(
    force_refresh: bool = False,
) -> list[ShoppingListEntry]:
    """Fetch and parse the shopping list. Uses 24h cache by default.

    Falls back to stale cache on network failure. Logs warning if cache > 7 days.
    """
    import httpx

    params = load_trading_params()
    sl_config = params.get("shopping_list", {})
    url = sl_config.get("url", "")
    ttl = sl_config.get("cache_ttl_hours", 24)

    # 1. Check cache
    if not force_refresh and _cache_is_fresh(ttl):
        log.info("shopping_list_from_cache")
        csv_text = _CACHE_FILE.read_text()
    else:
        # 2. Fetch from Google Sheets
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                csv_text = resp.text
                # Write cache
                _CACHE_FILE.write_text(csv_text)
                _TIMESTAMP_FILE.write_text(datetime.now().isoformat())
                log.info("shopping_list_fetched", bytes=len(csv_text))
        except Exception as e:
            log.warning("shopping_list_fetch_failed", error=str(e))
            if _CACHE_FILE.exists():
                csv_text = _CACHE_FILE.read_text()
                # Check if cache is dangerously old
                if _TIMESTAMP_FILE.exists():
                    try:
                        ts = datetime.fromisoformat(
                            _TIMESTAMP_FILE.read_text().strip()
                        )
                        age_days = (datetime.now() - ts).days
                        if age_days > 7:
                            log.error(
                                "shopping_list_stale_cache",
                                age_days=age_days,
                                msg="Cache > 7 days old — ratings may be unreliable",
                            )
                            # Telegram escalation per spec
                            try:
                                from src.delivery.telegram_bot import send_alert
                                import asyncio
                                asyncio.create_task(send_alert(
                                    f"⚠️ Shopping list cache is {age_days} days old. "
                                    f"Ratings may be unreliable. Re-fetch or update URL."
                                ))
                            except ImportError:
                                pass  # Telegram not wired yet
                    except (ValueError, OSError):
                        pass
                log.info("shopping_list_using_stale_cache")
            else:
                log.error("shopping_list_no_data")
                return []

    # 3. Parse
    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)
    # Skip header row
    if rows and rows[0] and rows[0][0].strip() == "Name":
        rows = rows[1:]

    entries = _parse_csv_rows(rows)
    log.info("shopping_list_parsed", entries=len(entries))
    return entries
```

- [ ] **Step 4: Add exports to src/data/__init__.py**

Add import after the existing imports:

```python
from src.data.shopping_list import fetch_shopping_list, resolve_ticker
```

Add to `__all__`:

```python
    # Shopping list
    "fetch_shopping_list",
    "resolve_ticker",
```

- [ ] **Step 5: Run parsing tests**

Run: `pytest tests/test_shopping_list.py::TestCSVParsing -v`
Expected: All tests pass

- [ ] **Step 6: Write failing test for fetch_shopping_list**

Append to `tests/test_shopping_list.py`:

```python
import asyncio


class TestFetchShoppingList:
    def test_fetch_from_cache(self, tmp_path, monkeypatch) -> None:
        """When cache is fresh, fetch_shopping_list reads from cache."""
        from src.data import shopping_list as sl_mod

        # Mock resolve_ticker to avoid yfinance calls
        monkeypatch.setattr(sl_mod, "resolve_ticker", lambda name: {
            "Alphabet": "GOOG",
        }.get(name.strip()))

        # Set up fresh cache
        cache_csv = (
            "Name,Rating,Date Updated,2026 Price Target,As of Date,2027 Price Target\n"
            "Alphabet,Buy,3/15/2026,200-220,,250-280\n"
        )
        cache_file = tmp_path / ".shopping_list_cache.csv"
        cache_file.write_text(cache_csv)
        ts_file = tmp_path / ".shopping_list_fetched"
        ts_file.write_text(datetime.now().isoformat())

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
```

- [ ] **Step 7: Run fetch tests**

Run: `pytest tests/test_shopping_list.py::TestFetchShoppingList -v`
Expected: All pass (cache test reads from file, no-data test returns empty)

- [ ] **Step 8: Run full test suite**

Run: `pytest tests/ -v --tb=short`
Expected: All tests pass

- [ ] **Step 9: Commit**

```bash
git add src/data/shopping_list.py src/data/__init__.py tests/test_shopping_list.py
git commit -m "feat: shopping list data layer — fetch, cache, parse, resolve tickers"
```

---

### Task 4: Conviction Modifier

Add `_apply_shopping_list_adjustment` to `src/main.py` and wire it into `build_recommendations`.

**Files:**
- Modify: `src/main.py:586-618` (after `_apply_tv_adjustment`)
- Modify: `src/main.py:388-583` (`build_recommendations` signature + wiring)
- Test: `tests/test_shopping_list.py` (append)

- [ ] **Step 1: Write failing tests for conviction adjustment**

Append to `tests/test_shopping_list.py`:

```python
from tests.fixtures import make_sized_opportunity
from src.models.shopping_list import ShoppingListEntry


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
        sized = make_sized_opportunity(conviction="low")
        entry = self._make_entry("Top Stock to Buy", 5)
        result, label = _apply_shopping_list_adjustment(sized, {"TEST": entry})
        assert result.conviction == "high"
        assert "Upgraded" in label
        assert "Parkev" in label

    def test_top_15_upgrades_by_1(self) -> None:
        from src.main import _apply_shopping_list_adjustment
        sized = make_sized_opportunity(conviction="medium")
        entry = self._make_entry("Top 15 Stock", 4)
        result, label = _apply_shopping_list_adjustment(sized, {"TEST": entry})
        assert result.conviction == "high"
        assert "Top 15" in label

    def test_buy_no_change(self) -> None:
        from src.main import _apply_shopping_list_adjustment
        sized = make_sized_opportunity(conviction="medium")
        entry = self._make_entry("Buy", 3)
        result, label = _apply_shopping_list_adjustment(sized, {"TEST": entry})
        assert result.conviction == "medium"
        assert label is None

    def test_hold_downgrades_by_1(self) -> None:
        from src.main import _apply_shopping_list_adjustment
        sized = make_sized_opportunity(conviction="high")
        entry = self._make_entry("Hold/ Market Perform", 1)
        result, label = _apply_shopping_list_adjustment(sized, {"TEST": entry})
        assert result.conviction == "medium"
        assert "Downgraded" in label

    def test_sell_downgrades_with_warning(self) -> None:
        from src.main import _apply_shopping_list_adjustment
        sized = make_sized_opportunity(conviction="medium")
        entry = self._make_entry("Sell", 0)
        result, label = _apply_shopping_list_adjustment(sized, {"TEST": entry})
        assert result.conviction == "low"
        assert "Sell-rated" in label

    def test_stale_entry_no_adjustment(self) -> None:
        from src.main import _apply_shopping_list_adjustment
        sized = make_sized_opportunity(conviction="low")
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
        sized = make_sized_opportunity(conviction="high")
        entry = self._make_entry("Top Stock to Buy", 5)
        result, label = _apply_shopping_list_adjustment(sized, {"TEST": entry})
        assert result.conviction == "high"  # already max
        assert label is None  # no change = no label

    def test_low_stays_low_on_downgrade_sell(self) -> None:
        from src.main import _apply_shopping_list_adjustment
        sized = make_sized_opportunity(conviction="low")
        entry = self._make_entry("Sell", 0)
        result, label = _apply_shopping_list_adjustment(sized, {"TEST": entry})
        # Sell on low → still low (can't go to skip via shopping list)
        assert result.conviction == "low"
        assert label is None  # no actual change
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_shopping_list.py::TestConvictionModifier -v`
Expected: FAIL with `ImportError` (function doesn't exist yet)

- [ ] **Step 3: Implement _apply_shopping_list_adjustment**

Add after `_apply_tv_adjustment` (after line 618) in `src/main.py`:

```python
def _apply_shopping_list_adjustment(
    sized: SizedOpportunity,
    shopping_list: dict[str, "ShoppingListEntry"],
) -> tuple[SizedOpportunity, str | None]:
    """Adjust conviction based on shopping list rating.

    Applied after TV adjustment. Stale entries (>90 days) are neutralized.
    Returns (sized_opportunity, label_or_none).
    """
    from src.models.shopping_list import ShoppingListEntry

    entry = shopping_list.get(sized.symbol)
    if not entry:
        return sized, None

    # Stale guard: no adjustment for old ratings
    if entry.stale:
        return sized, None

    current_idx = (
        _CONVICTION_LEVELS.index(sized.conviction)
        if sized.conviction in _CONVICTION_LEVELS
        else 1
    )
    original = sized.conviction

    tier = entry.rating_tier
    if tier == 5:
        new_idx = min(len(_CONVICTION_LEVELS) - 1, current_idx + 2)
    elif tier == 4:
        new_idx = min(len(_CONVICTION_LEVELS) - 1, current_idx + 1)
    elif tier in (3, 2):
        return sized, None  # Buy and Borderline Buy: no change
    elif tier == 1:
        new_idx = max(1, current_idx - 1)  # floor at low, not skip
    elif tier == 0:
        new_idx = max(1, current_idx - 1)  # floor at low, not skip
    else:
        return sized, None

    new_conviction = _CONVICTION_LEVELS[new_idx]
    if new_conviction == original:
        return sized, None

    sized.conviction = new_conviction

    # Generate label
    if tier == 5:
        label = "\u2B06 Upgraded (Top Stock \u2014 Parkev)"
    elif tier == 4:
        label = "\u2B06 Upgraded (Top 15 Stock \u2014 Parkev)"
    elif tier == 1:
        label = "\u2B07 Downgraded (Hold \u2014 Parkev)"
    elif tier == 0:
        label = "\u26A0 Sell-rated (Parkev)"
    else:
        label = None

    sized.conviction_label = label
    return sized, label
```

- [ ] **Step 4: Wire into build_recommendations**

Modify `build_recommendations` signature at line 388 to add the `shopping_list` parameter:

```python
def build_recommendations(
    all_signals: list[AlphaSignal],
    watchlist_data: list[tuple[str, MarketContext, PriceHistory, OptionsChain, EventCalendar]],
    portfolio: PortfolioState | None = None,
    intel_contexts: list[IntelligenceContext] | None = None,
    shopping_list: dict[str, ShoppingListEntry] | None = None,
) -> list[SizedOpportunity]:
```

Note: Add `from src.models.shopping_list import ShoppingListEntry` to the imports at the top of `src/main.py`.

After the TV adjustment for puts (line 473), add:

```python
        # Shopping list conviction adjustment
        if shopping_list:
            sized, _ = _apply_shopping_list_adjustment(sized, shopping_list)
```

After the TV adjustment for calls (line 522), add the same:

```python
        # Shopping list conviction adjustment
        if shopping_list:
            sized, _ = _apply_shopping_list_adjustment(sized, shopping_list)
```

Also in the TV-only covered call section (section 3, around line 575, just before `sized_symbols.add(symbol)`), apply the shopping list adjustment to the newly created `SizedOpportunity`:

```python
            # Shopping list conviction adjustment (TV-only calls have no TV adj step)
            if shopping_list:
                recommendations[-1], _ = _apply_shopping_list_adjustment(
                    recommendations[-1], shopping_list
                )
```

- [ ] **Step 5: Run conviction tests**

Run: `pytest tests/test_shopping_list.py::TestConvictionModifier -v`
Expected: All 9 tests pass

- [ ] **Step 6: Run full test suite**

Run: `pytest tests/ -v --tb=short`
Expected: All tests pass (shopping_list param defaults to None, backwards-compatible)

- [ ] **Step 7: Commit**

```bash
git add src/main.py tests/test_shopping_list.py
git commit -m "feat: conviction modifier — Parkev ratings adjust trade conviction"
```

---

### Task 5: Scanner Integration — Shopping List as Primary Universe

Modify `scan_wheel_candidates` to accept the shopping list as the primary discovery source, with Finviz as backfill.

**Files:**
- Modify: `src/main.py:175-189` (`ScannerPick` dataclass)
- Modify: `src/main.py:191-381` (`scan_wheel_candidates`)
- Test: `tests/test_shopping_list.py` (append)

- [ ] **Step 1: Write failing test for scanner with shopping list**

Append to `tests/test_shopping_list.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_shopping_list.py::TestScannerIntegration -v`
Expected: FAIL (fields don't exist on ScannerPick yet)

- [ ] **Step 3: Add fields to ScannerPick**

In `src/main.py`, add two optional fields to the `ScannerPick` dataclass (after `next_earnings` at line 188):

```python
    shopping_list_rating: str | None = None    # e.g. "Buy", "Top 15 Stock"
    price_target: str | None = None            # e.g. "$500-550"
```

- [ ] **Step 4: Modify scan_wheel_candidates signature**

Change the function signature at line 191. Keep the existing `watchlist_symbols` parameter name (matches call sites) and add `shopping_list`:

```python
def scan_wheel_candidates(
    watchlist_symbols: set[str],
    etrade_session: object | None = None,
    max_picks: int = 8,
    shopping_list: list[ShoppingListEntry] | None = None,
) -> list[ScannerPick]:
    """Discover and screen wheel candidates dynamically.

    When shopping_list is provided, uses it as the primary discovery universe.
    Falls back to Finviz if shopping list yields < 3 picks after screening.
    """
```

Note: The parameter stays `watchlist_symbols` to match existing call sites. All new code inside this function also uses `watchlist_symbols`.

- [ ] **Step 5: Add shopping list primary path**

Insert at the beginning of `scan_wheel_candidates` (after the imports, before Phase 1), replacing the current Phase 1:

```python
    from src.data.scanner_sources import discover_scanner_universe
    from src.data.market import calculate_iv_rank

    candidates: list[str] = []
    sl_metadata: dict[str, Any] = {}  # ticker → ShoppingListEntry

    # Phase 1: Discover candidates
    if shopping_list:
        # Shopping list is primary universe
        from src.models.shopping_list import ShoppingListEntry

        # Filter and score
        scored: list[tuple[str, float, ShoppingListEntry]] = []
        for entry in shopping_list:
            if entry.ticker in watchlist_symbols:
                continue
            if entry.rating_tier == 0:  # Sell
                continue

            # Composite score: rating_tier * 3 + freshness_bonus
            # (upside_normalized requires current price, done in Phase 2)
            freshness = 0.0
            if entry.date_updated:
                age = (date.today() - entry.date_updated).days
                if age <= 30:
                    freshness = 1.0
                elif age <= 60:
                    freshness = 0.5
            score = entry.rating_tier * 3 + freshness
            scored.append((entry.ticker, score, entry))

        scored.sort(key=lambda x: x[1], reverse=True)
        candidates = [s[0] for s in scored[:40]]
        sl_metadata = {s[0]: s[2] for s in scored}
        log.info("scanner_shopping_list_candidates", count=len(candidates))
    else:
        # Fallback: Finviz discovery
        candidates = discover_scanner_universe(watchlist_symbols, max_candidates=60)

    if not candidates:
        log.info("scanner_no_candidates")
        return []
```

Then keep the existing Phase 2 screening logic, but after creating each `ScannerPick`, attach shopping list metadata:

After the line that creates a `ScannerPick` and appends it to `picks`, add:

```python
        # Attach shopping list metadata if available
        if pick.symbol in sl_metadata:
            sl_entry = sl_metadata[pick.symbol]
            pick.shopping_list_rating = sl_entry.rating
            if sl_entry.price_target_2026:
                low, high = sl_entry.price_target_2026
                pick.price_target = f"${low:,.0f}-{high:,.0f}"
```

After the Phase 2 loop, add Finviz backfill logic:

```python
    # Backfill from Finviz if shopping list yielded < 3 picks
    if shopping_list and len(picks) < 3:
        log.info("scanner_backfilling_finviz", shopping_list_picks=len(picks))
        finviz_candidates = discover_scanner_universe(watchlist_symbols, max_candidates=60)
        # Screen Finviz candidates through Phase 2 (same logic)
        # ... existing screening applied to finviz_candidates ...
        # Append up to (max_picks - len(picks)) Finviz picks
```

Note: The Finviz backfill reuses the same Phase 2 screening. The implementer should extract the Phase 2 screening into a helper or loop through the Finviz candidates with the same logic. Keep it simple — duplicate the screening for the backfill rather than a premature abstraction.

- [ ] **Step 6: Run scanner tests**

Run: `pytest tests/test_shopping_list.py::TestScannerIntegration -v`
Expected: All pass

- [ ] **Step 7: Run full test suite**

Run: `pytest tests/ -v --tb=short`
Expected: All tests pass (new param defaults to None)

- [ ] **Step 8: Commit**

```bash
git add src/main.py tests/test_shopping_list.py
git commit -m "feat: scanner uses shopping list as primary universe, Finviz backfill"
```

---

### Task 6: Bench Builder

Create `src/analysis/bench.py` with the `build_bench` function that screens shopping list names with lightweight technicals.

**Files:**
- Create: `src/analysis/bench.py`
- Modify: `src/analysis/__init__.py`
- Test: `tests/test_shopping_list.py` (append)

- [ ] **Step 1: Write failing tests for bench builder**

Append to `tests/test_shopping_list.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_shopping_list.py::TestBenchBuilder -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement bench.py**

```python
# src/analysis/bench.py
"""Bench builder — screens shopping list names for the briefing."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import structlog

from src.models.shopping_list import BenchEntry, ShoppingListEntry

log = structlog.get_logger()


def _check_near_actionable(
    iv_rank: float,
    rsi: float,
    next_earnings: date | None,
    today: date | None = None,
) -> tuple[bool, str | None]:
    """Check if a bench name is near-actionable.

    Triggers: earnings <7d (blackout watch), RSI <35, IV rank >55.
    """
    if today is None:
        today = date.today()
    reasons: list[str] = []

    if next_earnings and 0 < (next_earnings - today).days <= 7:
        reasons.append(
            f"Earns {next_earnings.strftime('%b')} {next_earnings.day} "
            f"\u2014 IN BLACKOUT, watch for post-earnings entry"
        )
    if rsi < 35:
        reasons.append(f"RSI {rsi:.0f} \u2014 oversold pullback entry")
    if iv_rank > 55:
        reasons.append(f"IV rank {iv_rank:.0f} \u2014 premium rich")

    if reasons:
        return True, ". ".join(reasons)
    return False, None


def _upside_pct(
    entry: ShoppingListEntry, current_price: Decimal,
) -> float | None:
    """Calculate upside percentage from current price to 2026 target midpoint."""
    if entry.price_target_2026 and current_price > 0:
        midpoint = (entry.price_target_2026[0] + entry.price_target_2026[1]) / 2
        return float((midpoint - current_price) / current_price)
    return None


def _rank_and_filter(
    shopping_list: list[ShoppingListEntry],
    watchlist: set[str],
    scanner_symbols: set[str],
) -> list[ShoppingListEntry]:
    """Filter and rank shopping list entries for bench consideration.

    Excludes: watchlist, scanner picks, Sell-rated, no ticker.
    Returns top 30 by rating tier.
    """
    filtered: list[ShoppingListEntry] = []
    for entry in shopping_list:
        if entry.ticker in watchlist:
            continue
        if entry.ticker in scanner_symbols:
            continue
        if entry.rating_tier == 0:  # Sell
            continue
        if not entry.ticker:
            continue
        filtered.append(entry)

    # Sort by rating tier descending
    filtered.sort(key=lambda e: e.rating_tier, reverse=True)
    return filtered[:30]


async def build_bench(
    shopping_list: list[ShoppingListEntry],
    watchlist: set[str],
    scanner_symbols: set[str],
) -> list[BenchEntry]:
    """Build the bench — top shopping list names with lightweight technicals.

    Fetches current price, RSI, IV rank proxy, and next earnings via yfinance.
    Returns top 15 ranked by composite score.
    """
    import asyncio
    import yfinance as yf

    candidates = _rank_and_filter(shopping_list, watchlist, scanner_symbols)
    if not candidates:
        return []

    tickers = [c.ticker for c in candidates]
    entry_map = {c.ticker: c for c in candidates}

    # Batch fetch lightweight data (yf.download is sync — run in thread)
    try:
        data = await asyncio.to_thread(
            yf.download,
            tickers, period="3mo", interval="1d", group_by="ticker",
            auto_adjust=True, progress=False, threads=True,
        )
    except Exception as e:
        log.warning("bench_yf_download_failed", error=str(e))
        return []

    bench_entries: list[tuple[float, BenchEntry]] = []

    for ticker in tickers:
        entry = entry_map[ticker]
        try:
            if len(tickers) == 1:
                hist = data
            else:
                hist = data[ticker] if ticker in data.columns.get_level_values(0) else None
            if hist is None or hist.empty:
                continue

            close = hist["Close"].dropna()
            if len(close) < 14:
                continue

            current_price = Decimal(str(round(float(close.iloc[-1]), 2)))

            # RSI(14)
            delta = close.diff()
            gain = delta.where(delta > 0, 0.0).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
            rs = gain.iloc[-1] / loss.iloc[-1] if loss.iloc[-1] != 0 else 100
            rsi = float(100 - (100 / (1 + rs)))

            # IV rank proxy from 252-day HV
            if len(close) >= 20:
                returns = close.pct_change().dropna()
                hv_20 = float(returns.tail(20).std() * (252 ** 0.5) * 100)
                hv_max = float(returns.std() * (252 ** 0.5) * 100)
                hv_min = float(returns.tail(min(60, len(returns))).std() * (252 ** 0.5) * 100) * 0.5
                iv_rank = min(100.0, max(0.0, (hv_20 - hv_min) / (hv_max - hv_min) * 100)) if hv_max > hv_min else 50.0
            else:
                iv_rank = 50.0

            # Next earnings (yfinance — sync call, run in thread)
            next_earnings_date = None
            try:
                def _get_earnings(t: str) -> date | None:
                    tk = yf.Ticker(t)
                    cal = tk.calendar
                    if cal is not None and not cal.empty:
                        earnings_dates = cal.get("Earnings Date")
                        if earnings_dates is not None and len(earnings_dates) > 0:
                            ed = earnings_dates[0]
                            return ed.date() if hasattr(ed, 'date') else None
                    return None
                next_earnings_date = await asyncio.to_thread(_get_earnings, ticker)
            except Exception:
                pass

            upside = _upside_pct(entry, current_price)

            # Price target display string
            pt_display = None
            if entry.price_target_2026:
                low, high = entry.price_target_2026
                if low == high:
                    pt_display = f"{low:,.0f}"
                else:
                    pt_display = f"{low:,.0f}-{high:,.0f}"

            # Near-actionable check
            actionable, actionable_reason = _check_near_actionable(
                iv_rank=iv_rank, rsi=rsi, next_earnings=next_earnings_date,
            )

            # Composite score for ranking
            upside_norm = min(max(upside, 0.0), 1.0) if upside is not None else 0.0
            iv_norm = min(iv_rank / 100.0, 1.0)
            rsi_bonus = 2.0 if rsi < 30 else (1.0 if rsi < 40 else 0.0)
            composite = entry.rating_tier * 3 + upside_norm * 2 + iv_norm + rsi_bonus

            bench_entry = BenchEntry(
                ticker=ticker,
                name=entry.name,
                rating=entry.rating,
                current_price=current_price,
                price_target=pt_display,
                upside_pct=upside,
                iv_rank=iv_rank,
                rsi=rsi,
                next_earnings=next_earnings_date,
                near_actionable=actionable,
                actionable_reason=actionable_reason,
            )
            bench_entries.append((composite, bench_entry))

        except Exception as e:
            log.debug("bench_entry_failed", ticker=ticker, error=str(e))
            continue

    # Sort by composite score descending, return top 15
    bench_entries.sort(key=lambda x: x[0], reverse=True)
    result = [be for _, be in bench_entries[:15]]
    log.info("bench_built", entries=len(result))
    return result
```

- [ ] **Step 4: Add exports to src/analysis/__init__.py**

Add import:

```python
from src.analysis.bench import build_bench
```

Add to `__all__`:

```python
    "build_bench",
```

- [ ] **Step 5: Run bench tests**

Run: `pytest tests/test_shopping_list.py::TestBenchBuilder -v`
Expected: All 6 tests pass

- [ ] **Step 6: Run full test suite**

Run: `pytest tests/ -v --tb=short`
Expected: All tests pass

- [ ] **Step 7: Commit**

```bash
git add src/analysis/bench.py src/analysis/__init__.py tests/test_shopping_list.py
git commit -m "feat: bench builder — rank shopping list names with lightweight technicals"
```

---

### Task 7: Briefing Integration — BENCH Section + Scanner Header + Parkev Labels

Wire the BENCH section into the briefing formatter and update scanner header to show shopping list provenance.

**Files:**
- Modify: `src/main.py:683-697` (`format_local_briefing` signature)
- Modify: `src/main.py:1219-1317` (scanner picks rendering)
- Modify: `src/main.py:1318-1319` (insert BENCH section)
- Test: `tests/test_shopping_list.py` (append)

- [ ] **Step 1: Write failing test for bench rendering**

Append to `tests/test_shopping_list.py`:

```python
class TestBriefingIntegration:
    def test_bench_section_renders(self) -> None:
        """BENCH section appears in briefing when bench entries provided."""
        from src.main import format_local_briefing
        from src.models.market import MarketContext, PriceHistory, OptionsChain, EventCalendar
        from src.models.signals import AlphaSignal

        regime = MagicMock()
        regime.regime = "attack"

        bench = [
            BenchEntry(
                ticker="HIMS", name="Hims & Hers", rating="Buy",
                current_price=Decimal("42"), price_target="45-55",
                upside_pct=0.15, iv_rank=72.0, rsi=28.0,
                next_earnings=date(2026, 5, 5),
                near_actionable=True,
                actionable_reason="IV rank 72 — premium rich. RSI 28 — oversold pullback entry",
            ),
            BenchEntry(
                ticker="AVGO", name="Broadcom", rating="Top 15 Stock",
                current_price=Decimal("192"), price_target="400-440",
                upside_pct=1.29, iv_rank=38.0, rsi=55.0,
                next_earnings=date(2026, 5, 29),
                near_actionable=False, actionable_reason=None,
            ),
        ]

        briefing = format_local_briefing(
            regime=regime, vix=18.5, spy_change=0.003,
            all_signals=[], watchlist_data=[], tax_alerts=[],
            bench=bench,
        )
        assert "BENCH" in briefing
        assert "HIMS" in briefing
        assert "AVGO" in briefing
        # Near-actionable gets expanded treatment
        assert "READY" in briefing or "🔥" in briefing

    def test_conviction_label_in_recommendation(self) -> None:
        """Parkev label appears in recommendation rendering."""
        from src.main import format_local_briefing

        regime = MagicMock()
        regime.regime = "attack"

        recs = [make_sized_opportunity(
            conviction="high",
            conviction_label="⬆ Upgraded (Top 15 Stock — Parkev)",
        )]

        briefing = format_local_briefing(
            regime=regime, vix=18.5, spy_change=0.003,
            all_signals=[], watchlist_data=[], tax_alerts=[],
            recommendations=recs,
        )
        assert "Parkev" in briefing
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_shopping_list.py::TestBriefingIntegration -v`
Expected: FAIL (bench param doesn't exist yet on format_local_briefing)

- [ ] **Step 3: Add bench parameter to format_local_briefing**

In `src/main.py`, modify the `format_local_briefing` signature (line 683) to add `bench` parameter after `scanner_picks`:

```python
def format_local_briefing(
    regime: RegimeState,
    vix: float,
    spy_change: float,
    all_signals: list[AlphaSignal],
    watchlist_data: list[tuple[str, MarketContext, PriceHistory, OptionsChain, EventCalendar]],
    tax_alerts: list[str],
    recommendations: list[SizedOpportunity] | None = None,
    intel_contexts: list[IntelligenceContext] | None = None,
    analyst_brief: str | None = None,
    position_reviews: list[PositionReview] | None = None,
    tax_engine: Any | None = None,
    portfolio_state: Any | None = None,
    scanner_picks: list[ScannerPick] | None = None,
    bench: list[BenchEntry] | None = None,
) -> str:
```

- [ ] **Step 4: Add Parkev label rendering to recommendations**

Find where each recommendation is rendered in the briefing (the DO NOW / CONSIDER / OPPORTUNITIES sections). After the recommendation header line (the one with symbol, trade type, conviction), add:

```python
            if rec.conviction_label:
                lines.append(f"    {rec.conviction_label}")
```

- [ ] **Step 5: Update scanner header for shopping list provenance**

In the scanner picks section (around line 1316), replace the static header:

```python
    if scanner_lines:
        if not opportunities:
            lines.append(f"\n🎯 {_C.green(_C.bold('OPPORTUNITIES'))} "
                         f"— ${cash:,.0f} cash available")
        # Determine header based on whether picks came from shopping list
        has_sl_picks = scanner_picks and any(p.shopping_list_rating for p in scanner_picks)
        has_finviz = scanner_picks and any(p.shopping_list_rating is None for p in scanner_picks)
        if has_sl_picks and has_finviz:
            scanner_header = "from your shopping list + scanner"
        elif has_sl_picks:
            scanner_header = "from your shopping list"
        else:
            scanner_header = "high-IV names outside your watchlist"
        lines.append(f"\n  🔍 {_C.bold('SCANNER PICKS')} — {scanner_header}")
        lines.extend(scanner_lines)
```

Also, within the scanner pick rendering loop, after the reasons line, add price target display if available:

```python
            if pick.shopping_list_rating:
                rating_str = f" [{pick.shopping_list_rating}]"
            else:
                rating_str = ""
            # Modify the pick header line to include rating
            scanner_lines.append(f"  📝 {_C.cyan('SELL PUT')}: {_C.bold(pick.symbol)}{tier_str}{rating_str} @ ${pick.price:,.2f}")
```

And add price target after reasons:

```python
            if pick.price_target:
                # Calculate upside if we have current price
                scanner_lines[-1] += f" | Target: {pick.price_target}"
```

- [ ] **Step 6: Add BENCH section rendering**

Insert after the scanner section (after line ~1318, before REALLOCATE), add:

```python
    # ── BENCH — shopping list names approaching entry ──
    if bench:
        lines.append(f"\n📋 {_C.bold('BENCH')} — shopping list names approaching entry")
        for b in bench:
            # Price target + upside
            target_str = ""
            if b.price_target:
                if b.upside_pct is not None:
                    target_str = f" → ${b.price_target} ({b.upside_pct:+.0%})"
                else:
                    target_str = f" → ${b.price_target}"

            # Earnings display
            earns_str = ""
            if b.next_earnings:
                earns_str = f" | Earns {b.next_earnings.strftime('%b')} {b.next_earnings.day}"

            if b.near_actionable:
                # Expanded format with 🔥
                lines.append(
                    f"  🔥 {_C.bold(b.ticker):8s} {b.rating:12s} "
                    f"${b.current_price:<7,.0f}{target_str} "
                    f"| IV {b.iv_rank:.0f} | RSI {b.rsi:.0f}{earns_str}"
                )
                lines.append(f"     READY: {b.actionable_reason}")
            else:
                # Compact one-liner
                lines.append(
                    f"  {b.ticker:8s} {b.rating:12s} "
                    f"${b.current_price:<7,.0f}{target_str} "
                    f"| IV {b.iv_rank:.0f} | RSI {b.rsi:.0f}{earns_str}"
                )
```

- [ ] **Step 7: Run briefing tests**

Run: `pytest tests/test_shopping_list.py::TestBriefingIntegration -v`
Expected: All pass

- [ ] **Step 8: Run full test suite**

Run: `pytest tests/ -v --tb=short`
Expected: All tests pass

- [ ] **Step 9: Commit**

```bash
git add src/main.py tests/test_shopping_list.py
git commit -m "feat: BENCH section + scanner shopping list header + Parkev labels in briefing"
```

---

### Task 8: Orchestrator Wiring

Wire everything together in `run_analysis_cycle` in `src/main.py`.

**Files:**
- Modify: `src/main.py` (the `run_analysis_cycle` function — wherever scanner, recommendations, and briefing are called)

- [ ] **Step 1: Read the current run_analysis_cycle to identify exact insertion points**

Read `src/main.py` from the `run_analysis_cycle` function definition to understand the exact flow and line numbers. The orchestrator needs:
1. `shopping_list = await fetch_shopping_list()` — after portfolio loading, before recommendations
2. `shopping_list_by_ticker = {e.ticker: e for e in shopping_list}` — dict for conviction modifier
3. Pass `shopping_list=shopping_list_by_ticker` to `build_recommendations`
4. Pass `shopping_list=shopping_list` to `scan_wheel_candidates`
5. `bench = await build_bench(shopping_list, watchlist_set, {p.symbol for p in scanner_picks})` — after scanner
6. Pass `bench=bench` to `format_local_briefing`

- [ ] **Step 2: Add imports at top of main.py**

At the top of `src/main.py`, add:

```python
from src.data.shopping_list import fetch_shopping_list
from src.analysis.bench import build_bench
from src.models.shopping_list import BenchEntry, ShoppingListEntry
```

(`ShoppingListEntry` is used in `build_recommendations` and `scan_wheel_candidates` type hints; `BenchEntry` is used in `format_local_briefing` type hint.)

- [ ] **Step 3: Wire fetch_shopping_list into run_analysis_cycle**

After portfolio loading and before build_recommendations call, add:

```python
    # Load shopping list (cached daily)
    shopping_list = await fetch_shopping_list()
    shopping_list_by_ticker = {e.ticker: e for e in shopping_list}
    log.info("shopping_list_loaded", entries=len(shopping_list))
```

- [ ] **Step 4: Pass shopping_list to build_recommendations**

Find the `build_recommendations(...)` call and add the parameter:

```python
    recommendations = build_recommendations(
        all_signals, watchlist_data,
        portfolio=portfolio_state,
        intel_contexts=intel_contexts,
        shopping_list=shopping_list_by_ticker,
    )
```

- [ ] **Step 5: Pass shopping_list to scan_wheel_candidates**

Find the `scan_wheel_candidates(...)` call and add the parameter:

```python
    scanner_picks = scan_wheel_candidates(
        watchlist_set, etrade_session=etrade_session,
        shopping_list=shopping_list,
    )
```

- [ ] **Step 6: Add bench building after scanner**

After the scanner call:

```python
    bench = await build_bench(
        shopping_list,
        watchlist=watchlist_set,
        scanner_symbols={p.symbol for p in scanner_picks},
    )
```

- [ ] **Step 7: Pass bench to format_local_briefing**

Find the `format_local_briefing(...)` call and add:

```python
        bench=bench,
```

- [ ] **Step 8: Run full test suite**

Run: `pytest tests/ -v --tb=short`
Expected: All tests pass

- [ ] **Step 9: Commit**

```bash
git add src/main.py
git commit -m "feat: wire shopping list into analysis cycle — fetch, score, bench, display"
```

---

### Task 9: End-to-End Integration Test

Verify the full pipeline works with mocked data.

**Files:**
- Test: `tests/test_shopping_list.py` (append)

- [ ] **Step 1: Write integration test**

Append to `tests/test_shopping_list.py`:

```python
class TestIntegration:
    def test_full_pipeline_mock(self, tmp_path, monkeypatch) -> None:
        """Shopping list → scanner → conviction → bench → briefing."""
        from src.data import shopping_list as sl_mod

        # Mock resolve_ticker so no yfinance calls
        _mock_tickers = {"Alphabet": "GOOG", "Meta Platforms": "META", "Bad Corp": "BAD"}
        monkeypatch.setattr(sl_mod, "resolve_ticker", lambda name: _mock_tickers.get(name.strip()))

        # Set up cache with known data
        csv_content = (
            "Name,Rating,Date Updated,2026 Price Target,As of Date,2027 Price Target\n"
            "Alphabet,Top 15 Stock,4/1/2026,200-220,,250-280\n"
            "Meta Platforms,Buy,4/1/2026,700-800,,900-1000\n"
            "Bad Corp,Sell,4/1/2026,,,\n"
        )
        cache = tmp_path / ".shopping_list_cache.csv"
        cache.write_text(csv_content)
        ts = tmp_path / ".shopping_list_fetched"
        ts.write_text(datetime.now().isoformat())

        monkeypatch.setattr(sl_mod, "_CACHE_FILE", cache)
        monkeypatch.setattr(sl_mod, "_TIMESTAMP_FILE", ts)
        monkeypatch.setattr(
            sl_mod, "load_trading_params",
            lambda: {"shopping_list": {"url": "http://fake", "cache_ttl_hours": 24}},
        )

        entries = asyncio.run(sl_mod.fetch_shopping_list())
        # All 3 resolve (mocked): GOOG, META, BAD
        assert len(entries) == 3

        # Verify Alphabet resolved to GOOG
        goog = next((e for e in entries if e.ticker == "GOOG"), None)
        assert goog is not None
        assert goog.rating_tier == 4

        # Verify conviction modifier works
        from src.main import _apply_shopping_list_adjustment
        sl_dict = {e.ticker: e for e in entries}
        sized = make_sized_opportunity(conviction="low", symbol="GOOG")
        result, label = _apply_shopping_list_adjustment(sized, sl_dict)
        assert result.conviction == "medium"  # +1 for Top 15
        assert "Top 15" in label
```

- [ ] **Step 2: Run integration test**

Run: `pytest tests/test_shopping_list.py::TestIntegration -v`
Expected: PASS

- [ ] **Step 3: Run complete test suite one final time**

Run: `pytest tests/ -v --tb=short`
Expected: All tests pass, no regressions

- [ ] **Step 4: Commit**

```bash
git add tests/test_shopping_list.py
git commit -m "test: integration test for shopping list pipeline"
```
