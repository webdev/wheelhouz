"""Dynamic scanner universe discovery — no static lists.

Scrapes Finviz screener for high-volatility, optionable stocks with
liquid options and affordable collateral. Results cached daily.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import structlog

logger = structlog.get_logger()

CACHE_FILE = Path("config/.scanner_cache.json")
CACHE_TTL_HOURS = 12  # refresh twice per day

# Rate-limit: Finviz blocks aggressive scrapers
_LAST_REQUEST_TIME = 0.0
_MIN_REQUEST_INTERVAL = 2.0  # seconds between requests


def _rate_limit() -> None:
    """Enforce minimum interval between HTTP requests."""
    global _LAST_REQUEST_TIME
    elapsed = time.time() - _LAST_REQUEST_TIME
    if elapsed < _MIN_REQUEST_INTERVAL:
        time.sleep(_MIN_REQUEST_INTERVAL - elapsed)
    _LAST_REQUEST_TIME = time.time()


# ---------------------------------------------------------------------------
# Finviz screener
# ---------------------------------------------------------------------------

# Screener filters:
#   cap_smallover   = market cap >= $300M (filters penny-stock junk)
#   ind_stocksonly  = individual stocks only (no ETFs/ETNs)
#   sh_avgvol_o500  = average volume > 500K shares/day
#   sh_opt_option   = optionable (has listed options)
#   sh_price_o5     = price > $5
#   sh_price_u150   = price < $150 (affordable collateral)
#   ta_volatility_wo5 = weekly volatility > 5% (high-IV proxy)
#   o=-volatilityw  = sort by weekly volatility descending
#
# View v=171 = technical view (includes Beta, ATR, RSI, SMAs, 52W range)

_FINVIZ_BASE = (
    "https://finviz.com/screener.ashx?v=171"
    "&f=cap_smallover,ind_stocksonly,sh_avgvol_o500,"
    "sh_opt_option,sh_price_o5,sh_price_u150,ta_volatility_wo5"
    "&ft=4&o=-volatilityw"
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass
class FinvizCandidate:
    """A stock discovered via Finviz screening."""
    symbol: str
    price: float
    beta: float
    rsi: float
    sma20_dist: float  # % distance from 20-day SMA
    sma50_dist: float  # % distance from 50-day SMA
    high_52w_dist: float  # % below 52-week high (negative = below)
    volume: int


def _parse_pct(val: str) -> float:
    """Parse a percentage string like '12.34%' or '-5.67%' to float."""
    try:
        return float(val.strip().replace("%", "").replace(",", ""))
    except (ValueError, AttributeError):
        return 0.0


def _parse_number(val: str) -> float:
    """Parse a number string, handling commas."""
    try:
        return float(val.strip().replace(",", ""))
    except (ValueError, AttributeError):
        return 0.0


def _parse_int(val: str) -> int:
    """Parse an integer string, handling commas."""
    try:
        return int(val.strip().replace(",", ""))
    except (ValueError, AttributeError):
        return 0


def _scrape_finviz_page(url: str) -> tuple[list[FinvizCandidate], int]:
    """Scrape a single Finviz screener page.

    Returns (candidates, total_results).
    Technical view (v=171) columns:
    No | Ticker | Beta | ATR | SMA20 | SMA50 | SMA200 | 52W High | 52W Low
    | RSI | Price | Change | from Open | Gap | Volume
    """
    import requests
    from bs4 import BeautifulSoup

    _rate_limit()

    try:
        resp = requests.get(url, headers=_HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        logger.warning("finviz_request_failed", url=url[:80], error=str(e))
        return [], 0

    soup = BeautifulSoup(resp.text, "html.parser")

    # Total count: element with id "screener-total"
    total = 0
    total_el = soup.find(id="screener-total")
    if total_el:
        try:
            # Format: "#1 / 651 Total"
            total = int(total_el.text.split("/")[-1].strip().split()[0].replace(",", ""))
        except (ValueError, IndexError):
            pass

    # Data table: class "screener_table" (the actual results, not nav tables)
    candidates: list[FinvizCandidate] = []
    table = soup.find("table", class_="screener_table")
    if not table:
        logger.warning("finviz_table_not_found")
        return [], total

    rows = table.find_all("tr")
    for row in rows:
        tds = row.find_all("td")
        if len(tds) < 14:
            continue

        try:
            # Column 1 is the ticker (may be plain text or a link)
            ticker_el = tds[1].find("a") or tds[1]
            symbol = ticker_el.text.strip()
            if not symbol or not symbol.isalpha():
                continue

            # v=171 technical columns:
            # 0=No, 1=Ticker, 2=Beta, 3=ATR, 4=SMA20, 5=SMA50, 6=SMA200,
            # 7=52W High, 8=52W Low, 9=RSI, 10=Price, 11=Change,
            # 12=from Open, 13=Gap, 14=Volume
            beta = _parse_number(tds[2].text)
            sma20 = _parse_pct(tds[4].text)
            sma50 = _parse_pct(tds[5].text)
            high_52w = _parse_pct(tds[7].text)
            rsi = _parse_number(tds[9].text)
            price = _parse_number(tds[10].text)
            volume = _parse_int(tds[14].text) if len(tds) > 14 else 0

            if symbol and price > 0:
                candidates.append(FinvizCandidate(
                    symbol=symbol,
                    price=price,
                    beta=beta,
                    rsi=rsi,
                    sma20_dist=sma20,
                    sma50_dist=sma50,
                    high_52w_dist=high_52w,
                    volume=volume,
                ))
        except Exception:
            continue

    return candidates, total


def fetch_finviz_candidates(max_pages: int = 4) -> list[FinvizCandidate]:
    """Fetch high-volatility optionable stocks from Finviz screener.

    Scrapes up to max_pages (20 results per page) sorted by weekly
    volatility descending. Returns the most volatile candidates first.
    """
    all_candidates: list[FinvizCandidate] = []
    total = 0

    for page in range(max_pages):
        offset = page * 20 + 1  # Finviz uses 1-based offset
        url = f"{_FINVIZ_BASE}&r={offset}"
        candidates, page_total = _scrape_finviz_page(url)

        if page == 0:
            total = page_total
            logger.info("finviz_screener_total", total=total)

        if not candidates:
            break

        all_candidates.extend(candidates)
        logger.info("finviz_page_scraped", page=page + 1, found=len(candidates),
                     cumulative=len(all_candidates))

        # Stop if we've fetched enough or hit the end
        if len(all_candidates) >= total or len(candidates) < 20:
            break

    logger.info("finviz_fetch_complete", candidates=len(all_candidates))
    return all_candidates


# ---------------------------------------------------------------------------
# Cache layer
# ---------------------------------------------------------------------------

def _load_cache() -> dict | None:
    """Load cached scanner results if fresh enough."""
    if not CACHE_FILE.exists():
        return None

    try:
        data = json.loads(CACHE_FILE.read_text())
        cached_date = data.get("date", "")
        cached_hour = data.get("hour", 0)

        # Check if cache is from today and within TTL
        now_hour = __import__("datetime").datetime.now().hour
        if cached_date == date.today().isoformat():
            hours_old = now_hour - cached_hour
            if 0 <= hours_old < CACHE_TTL_HOURS:
                symbols = data.get("symbols", [])
                logger.info("scanner_cache_hit", symbols=len(symbols),
                            hours_old=hours_old)
                return data
    except Exception:
        pass

    return None


def _save_cache(symbols: list[str], metadata: list[dict]) -> None:
    """Save scanner results to cache."""
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    from datetime import datetime
    CACHE_FILE.write_text(json.dumps({
        "date": date.today().isoformat(),
        "hour": datetime.now().hour,
        "symbols": symbols,
        "metadata": metadata,
    }, indent=2))
    logger.info("scanner_cache_saved", symbols=len(symbols))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def discover_scanner_universe(
    watchlist: set[str],
    max_candidates: int = 60,
) -> list[str]:
    """Discover wheel-friendly stocks dynamically. No static lists.

    Pipeline:
    1. Check daily cache
    2. If stale, scrape Finviz for high-vol optionable stocks
    3. Filter out watchlist names, overbought, penny stocks
    4. Return symbols sorted by attractiveness

    Args:
        watchlist: symbols already on the user's watchlist (excluded)
        max_candidates: max symbols to return for detailed screening

    Returns:
        List of ticker symbols to screen for wheel opportunities.
    """
    # 1. Check cache
    cache = _load_cache()
    if cache:
        cached_symbols = [s for s in cache["symbols"] if s not in watchlist]
        if cached_symbols:
            return cached_symbols[:max_candidates]

    # 2. Fetch from Finviz
    logger.info("scanner_discovering_universe")
    finviz_results = fetch_finviz_candidates(max_pages=4)

    if not finviz_results:
        logger.warning("scanner_no_candidates_found")
        return []

    # 3. Filter, deduplicate, and score
    seen: set[str] = set()
    scored: list[tuple[str, float, dict]] = []
    for c in finviz_results:
        # Deduplicate
        if c.symbol in seen:
            continue
        seen.add(c.symbol)

        # Skip watchlist names
        if c.symbol in watchlist:
            continue

        # Skip overbought (RSI > 70) — bad time to sell puts
        if c.rsi > 70:
            continue

        # Skip very low RSI too (< 15) — might be falling knife
        if 0 < c.rsi < 15:
            continue

        # Score: prefer pullbacks with high volatility
        score = 0.0

        # RSI sweet spot for put selling: 25-50
        if 25 <= c.rsi <= 45:
            score += 3  # pullback territory
        elif c.rsi < 25:
            score += 2  # oversold, riskier but juicy premium
        elif c.rsi <= 55:
            score += 1  # neutral

        # Near support (below SMAs = potential bounce)
        if -15 <= c.sma50_dist <= 5:
            score += 1
        if c.sma20_dist < 0:
            score += 1  # below 20 SMA, mean reversion opportunity

        # Not at 52-week high (want discount entries)
        if c.high_52w_dist < -20:
            score += 2  # well below highs
        elif c.high_52w_dist < -10:
            score += 1

        # Affordable collateral bonus
        if c.price <= 30:
            score += 2
        elif c.price <= 60:
            score += 1

        # Beta bonus (higher beta = more premium)
        if c.beta >= 2.0:
            score += 1

        metadata = {
            "symbol": c.symbol,
            "price": c.price,
            "rsi": c.rsi,
            "beta": c.beta,
            "sma50_dist": c.sma50_dist,
            "high_52w_dist": c.high_52w_dist,
        }
        scored.append((c.symbol, score, metadata))

    # Sort by score descending
    scored.sort(key=lambda x: x[1], reverse=True)

    symbols = [s[0] for s in scored[:max_candidates]]
    metadata = [s[2] for s in scored[:max_candidates]]

    # 4. Cache results
    if symbols:
        _save_cache(symbols, metadata)

    logger.info("scanner_universe_discovered", candidates=len(symbols))
    return symbols
