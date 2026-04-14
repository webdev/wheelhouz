"""Market data via yfinance — IV rank, price history, technicals.

E*Trade doesn't provide IV rank, so we calculate it from yfinance
historical volatility (252 trading days). Also builds PriceHistory
and MarketContext models for the analysis engine.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import structlog
import yfinance as yf

from src.models.market import OptionContract, MarketContext, OptionsChain, PriceHistory

logger = structlog.get_logger()


# ── IV Rank / Percentile ────────────────────────────────────────


def calculate_iv_rank(
    symbol: str,
    current_iv: float,
    lookback_days: int = 252,
) -> dict[str, float]:
    """Calculate IV rank and IV percentile from historical realized vol.

    IV Rank = (current_iv - 52w_low) / (52w_high - 52w_low) * 100
    IV Percentile = % of days in lookback where IV was below current

    Uses realized vol as proxy since historical IV isn't freely available.
    """
    ticker = yf.Ticker(symbol)
    hist = ticker.history(period="1y")

    if hist.empty or len(hist) < 60:
        logger.warning("iv_rank_insufficient_data", symbol=symbol)
        return {
            "iv_rank": 50.0,
            "iv_percentile": 50.0,
            "hv_30d": 0.0,
            "iv_30d": current_iv * 100,
            "iv_hv_spread": 0.0,
        }

    # Log returns → 30-day rolling realized vol (annualized)
    closes = hist["Close"]
    log_returns = (closes / closes.shift(1)).apply(math.log).dropna()
    rolling_rv = log_returns.rolling(window=30).std() * math.sqrt(252) * 100
    rolling_rv = rolling_rv.dropna()

    if len(rolling_rv) < 30:
        return {
            "iv_rank": 50.0,
            "iv_percentile": 50.0,
            "hv_30d": 0.0,
            "iv_30d": current_iv * 100,
            "iv_hv_spread": 0.0,
        }

    rv_min = float(rolling_rv.min())
    rv_max = float(rolling_rv.max())
    hv_30d = float(rolling_rv.iloc[-1])

    # Use broker-supplied IV if available, otherwise use HV as proxy
    if current_iv > 0:
        iv_as_pct = current_iv * 100
    else:
        iv_as_pct = hv_30d  # HV-based proxy

    # IV Rank
    if rv_max - rv_min > 0:
        iv_rank = (iv_as_pct - rv_min) / (rv_max - rv_min) * 100
    else:
        iv_rank = 50.0

    # IV Percentile
    iv_pctile = float((rolling_rv < iv_as_pct).sum()) / len(rolling_rv) * 100

    return {
        "iv_rank": round(max(0.0, min(100.0, iv_rank)), 1),
        "iv_percentile": round(max(0.0, min(100.0, iv_pctile)), 1),
        "hv_30d": round(hv_30d, 2),
        "iv_30d": round(iv_as_pct, 2),
        "iv_hv_spread": round(iv_as_pct - hv_30d, 2),
    }


# ── RSI ─────────────────────────────────────────────────────────


def _calculate_rsi(closes: list[float], period: int = 14) -> float | None:
    """Calculate RSI(period) from a list of closing prices."""
    if len(closes) < period + 1:
        return None

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    recent = deltas[-(period):]

    gains = [d for d in recent if d > 0]
    losses = [-d for d in recent if d < 0]

    avg_gain = sum(gains) / period if gains else 0.0
    avg_loss = sum(losses) / period if losses else 0.0

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 1)


# ── Build shared models ─────────────────────────────────────────


def fetch_price_history(symbol: str) -> PriceHistory:
    """Build a PriceHistory from yfinance data (252 trading days)."""
    ticker = yf.Ticker(symbol)
    hist = ticker.history(period="1y")

    if hist.empty:
        logger.warning("price_history_empty", symbol=symbol)
        return PriceHistory(symbol=symbol, current_price=Decimal("0"))

    closes_float = [float(c) for c in hist["Close"]]
    volumes_float = [float(v) for v in hist["Volume"]]
    closes_decimal = [Decimal(str(round(c, 2))) for c in closes_float]

    current = closes_float[-1] if closes_float else 0.0

    def _sma(n: int) -> Decimal | None:
        if len(closes_float) < n:
            return None
        return Decimal(str(round(sum(closes_float[-n:]) / n, 2)))

    def _ema(n: int) -> Decimal | None:
        if len(closes_float) < n:
            return None
        multiplier = 2.0 / (n + 1)
        ema = closes_float[0]
        for price in closes_float[1:]:
            ema = (price - ema) * multiplier + ema
        return Decimal(str(round(ema, 2)))

    # Swing high/low over last 20 days
    recent_20 = closes_float[-20:] if len(closes_float) >= 20 else closes_float
    swing_high = Decimal(str(round(max(recent_20), 2))) if recent_20 else None
    swing_low = Decimal(str(round(min(recent_20), 2))) if recent_20 else None

    return PriceHistory(
        symbol=symbol,
        current_price=Decimal(str(round(current, 2))),
        sma_200=_sma(200),
        sma_50=_sma(50),
        sma_20=_sma(20),
        ema_9=_ema(9),
        high_52w=Decimal(str(round(max(closes_float), 2))),
        low_52w=Decimal(str(round(min(closes_float), 2))),
        recent_swing_high=swing_high,
        recent_swing_low=swing_low,
        rsi_14=_calculate_rsi(closes_float),
        daily_closes=closes_decimal,
        daily_volumes=volumes_float,
    )


def fetch_market_context(
    symbol: str,
    current_iv: float = 0.0,
) -> MarketContext:
    """Build a full MarketContext for a symbol.

    Combines yfinance price data with IV rank calculation.
    Pass current_iv from the E*Trade option chain if available.
    """
    ticker = yf.Ticker(symbol)
    hist = ticker.history(period="5d")

    if hist.empty:
        logger.warning("market_context_empty", symbol=symbol)
        return MarketContext(
            symbol=symbol, iv_rank=0, iv_percentile=0,
            iv_rank_change_5d=0, iv_30d=0, hv_30d=0, iv_hv_spread=0,
            price=Decimal("0"), price_change_1d=0, price_change_5d=0,
            price_vs_52w_high=0, price_vs_200sma=0,
            put_call_ratio=0, option_volume_vs_avg=0,
        )

    closes = [float(c) for c in hist["Close"]]
    price = closes[-1]
    price_1d_ago = closes[-2] if len(closes) >= 2 else price
    price_5d_ago = closes[0] if len(closes) >= 5 else price

    change_1d = ((price - price_1d_ago) / price_1d_ago * 100) if price_1d_ago else 0.0
    change_5d = ((price - price_5d_ago) / price_5d_ago * 100) if price_5d_ago else 0.0

    # 52w high and 200 SMA from longer history
    full_hist = ticker.history(period="1y")
    full_closes = [float(c) for c in full_hist["Close"]] if not full_hist.empty else [price]
    high_52w = max(full_closes)
    vs_52w_high = ((price - high_52w) / high_52w * 100) if high_52w else 0.0

    sma_200 = sum(full_closes[-200:]) / min(len(full_closes), 200) if full_closes else price
    vs_200sma = ((price - sma_200) / sma_200 * 100) if sma_200 else 0.0

    # IV rank — always calculate, using HV proxy if broker IV unavailable
    iv_data = calculate_iv_rank(symbol, current_iv)

    # VIX context
    vix_val: float | None = None
    vix_change: float | None = None
    vix_term: str | None = None
    try:
        vix = yf.Ticker("^VIX")
        vix_hist = vix.history(period="5d")
        if not vix_hist.empty:
            vix_closes = [float(c) for c in vix_hist["Close"]]
            vix_val = vix_closes[-1]
            vix_change = vix_closes[-1] - vix_closes[-2] if len(vix_closes) >= 2 else 0.0
        # VIX term structure: compare VIX to VIX3M
        vix3m = yf.Ticker("^VIX3M")
        vix3m_hist = vix3m.history(period="2d")
        if not vix3m_hist.empty and vix_val:
            vix3m_val = float(vix3m_hist["Close"].iloc[-1])
            vix_term = "contango" if vix_val < vix3m_val else "backwardation"
    except Exception:
        pass  # VIX data is supplementary, don't fail

    return MarketContext(
        symbol=symbol,
        iv_rank=iv_data["iv_rank"],
        iv_percentile=iv_data["iv_percentile"],
        iv_rank_change_5d=0.0,  # requires historical IV rank tracking
        iv_30d=iv_data["iv_30d"],
        hv_30d=iv_data["hv_30d"],
        iv_hv_spread=iv_data["iv_hv_spread"],
        price=Decimal(str(round(price, 2))),
        price_change_1d=round(change_1d, 2),
        price_change_5d=round(change_5d, 2),
        price_vs_52w_high=round(vs_52w_high, 2),
        price_vs_200sma=round(vs_200sma, 2),
        put_call_ratio=0.0,  # requires separate data source (CBOE)
        option_volume_vs_avg=0.0,
        vix=vix_val,
        vix_change_1d=vix_change,
        vix_term_structure=vix_term,
    )


def fetch_options_chain(symbol: str, target_dte: int = 30) -> OptionsChain:
    """Fetch real options chain from yfinance for nearest monthly expiration."""
    ticker = yf.Ticker(symbol)
    try:
        expirations_str = ticker.options
    except Exception:
        logger.warning("options_chain_unavailable", symbol=symbol)
        return OptionsChain(symbol=symbol)

    if not expirations_str:
        return OptionsChain(symbol=symbol)

    today = date.today()
    exp_dates = [date.fromisoformat(e) for e in expirations_str]
    target_date = today + timedelta(days=target_dte)
    best_exp = min(exp_dates, key=lambda d: abs((d - target_date).days))

    try:
        chain = ticker.option_chain(best_exp.isoformat())
    except Exception as e:
        logger.warning("options_chain_fetch_failed", symbol=symbol, error=str(e))
        return OptionsChain(symbol=symbol, expirations=exp_dates)

    def _parse_contracts(df: Any, option_type: str) -> list[OptionContract]:
        contracts = []
        for _, row in df.iterrows():
            try:
                contracts.append(OptionContract(
                    strike=Decimal(str(round(float(row["strike"]), 2))),
                    expiration=best_exp,
                    option_type=option_type,
                    bid=Decimal(str(round(float(row.get("bid", 0)), 2))),
                    ask=Decimal(str(round(float(row.get("ask", 0)), 2))),
                    mid=Decimal(str(round((float(row.get("bid", 0)) + float(row.get("ask", 0))) / 2, 2))),
                    volume=int(row.get("volume", 0) or 0),
                    open_interest=int(row.get("openInterest", 0) or 0),
                    implied_vol=float(row.get("impliedVolatility", 0) or 0),
                    delta=0.0,
                ))
            except (ValueError, KeyError):
                continue
        return contracts

    puts = _parse_contracts(chain.puts, "put")
    calls = _parse_contracts(chain.calls, "call")

    atm_iv = None
    if puts:
        hist = ticker.history(period="1d")
        if not hist.empty:
            current_price = float(hist["Close"].iloc[-1])
            atm_put = min(puts, key=lambda c: abs(float(c.strike) - current_price))
            atm_iv = atm_put.implied_vol

    return OptionsChain(
        symbol=symbol,
        puts=puts,
        calls=calls,
        atm_iv=atm_iv,
        expirations=exp_dates,
    )
