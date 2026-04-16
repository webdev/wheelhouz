# src/data/tradingview.py
"""TradingView technical analysis consensus via tradingview-ta.

Fetches the same buy/sell/neutral summary millions of traders see.
Results cached to disk with 30-minute TTL to avoid rate limiting.
Requests are throttled (1s delay) to stay under unofficial API limits.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import structlog
from tradingview_ta import TA_Handler, Interval

from src.models.intelligence import TechnicalConsensus

logger = structlog.get_logger()

# Disk cache: survives between runs
_CACHE_DIR = Path(__file__).parent.parent.parent / "config" / ".tv_cache"
_CACHE_TTL = 1800  # 30 minutes

# Rate limiting: 1 second between requests
_LAST_REQUEST_TIME: float = 0.0
_REQUEST_DELAY = 1.5

# Symbols listed on NYSE rather than NASDAQ
_NYSE_SYMBOLS = {
    "PLTR", "CRM", "UBER", "TSM", "JPM", "WFC", "BAC", "GS", "V", "MA",
    "DG", "KO", "CL", "T", "BA", "CVX", "CCL", "CLX", "CVS", "MCD", "CELH", "BROS",
    "WMT", "PG", "JNJ", "XOM", "PFE", "UNH", "HD", "DIS", "IBM", "GE",
}

# In-memory cache for the current session
_mem_cache: dict[str, tuple[float, TechnicalConsensus]] = {}


def _get_exchange(symbol: str) -> str:
    """Return the exchange for a given symbol."""
    return "NYSE" if symbol in _NYSE_SYMBOLS else "NASDAQ"


def _cache_path(symbol: str) -> Path:
    """Return the cache file path for a symbol."""
    return _CACHE_DIR / f"{symbol}.json"


def _read_disk_cache(symbol: str) -> TechnicalConsensus | None:
    """Read cached consensus from disk if fresh enough."""
    path = _cache_path(symbol)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if time.time() - data["ts"] < _CACHE_TTL:
            return TechnicalConsensus(
                source="tradingview",
                overall=data["overall"],
                oscillators=data["oscillators"],
                moving_averages=data["moving_averages"],
                buy_count=data["buy_count"],
                neutral_count=data["neutral_count"],
                sell_count=data["sell_count"],
                raw_indicators=data.get("raw_indicators", {}),
            )
    except Exception:
        pass
    return None


def _write_disk_cache(symbol: str, tc: TechnicalConsensus) -> None:
    """Write consensus to disk cache."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "ts": time.time(),
        "overall": tc.overall,
        "oscillators": tc.oscillators,
        "moving_averages": tc.moving_averages,
        "buy_count": tc.buy_count,
        "neutral_count": tc.neutral_count,
        "sell_count": tc.sell_count,
        "raw_indicators": tc.raw_indicators,
    }
    _cache_path(symbol).write_text(json.dumps(data))


def fetch_tradingview_consensus(symbol: str) -> TechnicalConsensus | None:
    """Fetch TradingView technical analysis for a US stock.

    Returns None on any failure (rate limit, network, bad symbol).
    Cached to disk for 30 minutes. 1-second delay between API calls.
    """
    global _LAST_REQUEST_TIME
    now = time.time()

    # 1. Check in-memory cache
    if symbol in _mem_cache:
        ts, cached = _mem_cache[symbol]
        if now - ts < _CACHE_TTL:
            return cached

    # 2. Check disk cache
    disk_result = _read_disk_cache(symbol)
    if disk_result is not None:
        _mem_cache[symbol] = (now, disk_result)
        return disk_result

    # 3. Fetch from TradingView with rate limiting
    elapsed = now - _LAST_REQUEST_TIME
    if elapsed < _REQUEST_DELAY:
        time.sleep(_REQUEST_DELAY - elapsed)

    try:
        handler = TA_Handler(
            symbol=symbol,
            screener="america",
            exchange=_get_exchange(symbol),
            interval=Interval.INTERVAL_1_DAY,
        )
        analysis = handler.get_analysis()
        _LAST_REQUEST_TIME = time.time()

        summary = analysis.summary
        oscillators = analysis.oscillators
        moving_averages = analysis.moving_averages
        indicators = analysis.indicators or {}

        result = TechnicalConsensus(
            source="tradingview",
            overall=summary.get("RECOMMENDATION", "NEUTRAL"),
            oscillators=oscillators.get("RECOMMENDATION", "NEUTRAL"),
            moving_averages=moving_averages.get("RECOMMENDATION", "NEUTRAL"),
            buy_count=int(summary.get("BUY", 0)),
            neutral_count=int(summary.get("NEUTRAL", 0)),
            sell_count=int(summary.get("SELL", 0)),
            raw_indicators={k: float(v) for k, v in indicators.items()
                           if isinstance(v, (int, float)) and v is not None},
        )

        _mem_cache[symbol] = (time.time(), result)
        _write_disk_cache(symbol, result)
        logger.info("tradingview_fetched", symbol=symbol, overall=result.overall)
        return result

    except Exception as e:
        logger.warning("tradingview_failed", symbol=symbol, error=str(e))
        return None
