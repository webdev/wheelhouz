# src/data/tradingview.py
"""TradingView technical analysis consensus via tradingview-ta.

Fetches the same buy/sell/neutral summary millions of traders see.
Results cached with 1-hour TTL to avoid rate limiting (unofficial API).
"""
from __future__ import annotations

import time

import structlog
from tradingview_ta import TA_Handler, Interval

from src.models.intelligence import TechnicalConsensus

logger = structlog.get_logger()

# Simple TTL cache: {symbol: (timestamp, TechnicalConsensus)}
_tv_cache: dict[str, tuple[float, TechnicalConsensus]] = {}
_CACHE_TTL = 3600  # 1 hour

# Symbols listed on NYSE rather than NASDAQ
_NYSE_SYMBOLS = {"PLTR", "CRM", "UBER", "TSM", "JPM", "WFC", "BAC", "GS", "V", "MA"}


def _get_exchange(symbol: str) -> str:
    """Return the exchange for a given symbol."""
    return "NYSE" if symbol in _NYSE_SYMBOLS else "NASDAQ"


def fetch_tradingview_consensus(symbol: str) -> TechnicalConsensus | None:
    """Fetch TradingView technical analysis for a US stock.

    Returns None on any failure (rate limit, network, bad symbol).
    Cached for 1 hour per symbol.
    """
    now = time.time()
    if symbol in _tv_cache:
        ts, cached = _tv_cache[symbol]
        if now - ts < _CACHE_TTL:
            return cached

    try:
        handler = TA_Handler(
            symbol=symbol,
            screener="america",
            exchange=_get_exchange(symbol),
            interval=Interval.INTERVAL_1_DAY,
        )
        analysis = handler.get_analysis()

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

        _tv_cache[symbol] = (now, result)
        logger.info("tradingview_fetched", symbol=symbol, overall=result.overall)
        return result

    except Exception as e:
        logger.warning("tradingview_failed", symbol=symbol, error=str(e))
        return None
