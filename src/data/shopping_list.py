"""Shopping list data layer — fetch, cache, parse, resolve tickers."""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime, timedelta, timezone
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
    "Corning": "GLW",
    "Exxon": "XOM",
    "Cameco": "CCJ",
    "PACCAR": "PCAR",
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
    match = re.match(r"^(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)$", cleaned)
    if match:
        try:
            return (Decimal(match.group(1)), Decimal(match.group(2)))
        except InvalidOperation:
            return None
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

    WARNING: This function is synchronous and may block on yfinance I/O.
    Call from a thread (asyncio.to_thread) when used in async context.
    """
    stripped = name.strip()
    if stripped in _MANUAL_OVERRIDES:
        return _MANUAL_OVERRIDES[stripped]

    ticker_map = _load_ticker_map()
    if stripped in ticker_map:
        return ticker_map[stripped]

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
    """Load the persistent name-to-ticker cache."""
    if _TICKER_MAP_FILE.exists():
        try:
            return json.loads(_TICKER_MAP_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_ticker_map(name: str, ticker: str) -> None:
    """Save a name-to-ticker mapping to the persistent cache."""
    ticker_map = _load_ticker_map()
    ticker_map[name] = ticker
    _TICKER_MAP_FILE.write_text(json.dumps(ticker_map, indent=2))


def _parse_csv_rows(
    rows: list[list[str]],
    today: date | None = None,
) -> list[ShoppingListEntry]:
    """Parse CSV data rows into ShoppingListEntry objects.

    Columns: 0=Name, 1=Rating, 2=Date Updated, 3=2026 Target, 4=As-of Date (skipped), 5=2027 Target.
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
        return datetime.now(timezone.utc) - ts < timedelta(hours=ttl_hours)
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

    if not force_refresh and _cache_is_fresh(ttl):
        log.info("shopping_list_from_cache")
        csv_text = _CACHE_FILE.read_text()
    else:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                csv_text = resp.text
                _CACHE_FILE.write_text(csv_text)
                _TIMESTAMP_FILE.write_text(datetime.now(timezone.utc).isoformat())
                log.info("shopping_list_fetched", bytes=len(csv_text))
        except Exception as e:
            log.warning("shopping_list_fetch_failed", error=str(e))
            if _CACHE_FILE.exists():
                csv_text = _CACHE_FILE.read_text()
                if _TIMESTAMP_FILE.exists():
                    try:
                        ts = datetime.fromisoformat(
                            _TIMESTAMP_FILE.read_text().strip()
                        )
                        age_days = (datetime.now(timezone.utc) - ts).days
                        if age_days > 7:
                            log.error(
                                "shopping_list_stale_cache",
                                age_days=age_days,
                                msg="Cache > 7 days old — ratings may be unreliable",
                            )
                            try:
                                from src.delivery.telegram_bot import send_alert
                                import asyncio
                                asyncio.create_task(send_alert(
                                    f"⚠️ Shopping list cache is {age_days} days old. "
                                    f"Ratings may be unreliable. Re-fetch or update URL."
                                ))
                            except ImportError:
                                pass
                    except (ValueError, OSError):
                        pass
                log.info("shopping_list_using_stale_cache")
            else:
                log.error("shopping_list_no_data")
                return []

    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)
    if rows and rows[0] and rows[0][0].strip() == "Name":
        rows = rows[1:]

    import asyncio
    entries = await asyncio.to_thread(_parse_csv_rows, rows)
    log.info("shopping_list_parsed", entries=len(entries))
    return entries
