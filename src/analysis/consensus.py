# src/analysis/consensus.py
"""Self-calculated technical consensus engine — replaces TradingView API calls.

Computes oscillator and moving-average votes from local OHLCV data using
pandas-ta. Zero external API calls. Works with 20+ bars of data.
"""
from __future__ import annotations

from typing import Any

import pandas as pd
import pandas_ta as ta

from src.models.intelligence import TechnicalConsensus

# Vote string constants
_BUY = "BUY"
_SELL = "SELL"
_NEUTRAL = "NEUTRAL"

# Consensus thresholds
_STRONG_THRESHOLD = 0.80
_NORMAL_THRESHOLD = 0.60


def _vote_ratio(votes: list[str], target: str) -> float:
    """Return fraction of votes matching target, or 0 if no votes."""
    if not votes:
        return 0.0
    return sum(1 for v in votes if v == target) / len(votes)


def _votes_to_label(votes: list[str]) -> str:
    """Map vote list to STRONG_BUY / BUY / NEUTRAL / SELL / STRONG_SELL."""
    buy_ratio = _vote_ratio(votes, _BUY)
    sell_ratio = _vote_ratio(votes, _SELL)

    if buy_ratio >= _STRONG_THRESHOLD:
        return "STRONG_BUY"
    if buy_ratio >= _NORMAL_THRESHOLD:
        return _BUY
    if sell_ratio >= _STRONG_THRESHOLD:
        return "STRONG_SELL"
    if sell_ratio >= _NORMAL_THRESHOLD:
        return _SELL
    return _NEUTRAL


def _label_to_score(label: str) -> float:
    """Map consensus label to numeric score for averaging."""
    return {
        "STRONG_BUY": 2.0,
        "BUY": 1.0,
        "NEUTRAL": 0.0,
        "SELL": -1.0,
        "STRONG_SELL": -2.0,
    }.get(label, 0.0)


def _score_to_label(score: float) -> str:
    """Map averaged numeric score back to consensus label."""
    if score >= 1.5:
        return "STRONG_BUY"
    if score >= 0.5:
        return "BUY"
    if score <= -1.5:
        return "STRONG_SELL"
    if score <= -0.5:
        return "SELL"
    return "NEUTRAL"


def _safe_float(series: pd.Series | None, idx: int = -1) -> float | None:
    """Extract a scalar from a Series, returning None on missing/NaN."""
    if series is None:
        return None
    try:
        val = series.iloc[idx]
        return float(val) if pd.notna(val) else None
    except (IndexError, TypeError, ValueError):
        return None


# ── Oscillator votes ──────────────────────────────────────────────────────────

def _vote_rsi(close: pd.Series) -> tuple[str, float | None]:
    """RSI(14): <30 BUY, >70 SELL, else NEUTRAL."""
    rsi_series = ta.rsi(close, length=14)
    val = _safe_float(rsi_series)
    if val is None:
        return _NEUTRAL, None
    if val < 30:
        return _BUY, val
    if val > 70:
        return _SELL, val
    return _NEUTRAL, val


def _vote_stoch(high: pd.Series, low: pd.Series, close: pd.Series) -> tuple[str, float | None]:
    """Stochastic %K(14,3,3): <20 BUY, >80 SELL, else NEUTRAL."""
    stoch = ta.stoch(high, low, close, k=14, d=3, smooth_k=3)
    if stoch is None or stoch.empty:
        return _NEUTRAL, None
    # pandas-ta returns columns like STOCHk_14_3_3 and STOCHd_14_3_3
    k_col = [c for c in stoch.columns if c.startswith("STOCHk")]
    if not k_col:
        return _NEUTRAL, None
    val = _safe_float(stoch[k_col[0]])
    if val is None:
        return _NEUTRAL, None
    if val < 20:
        return _BUY, val
    if val > 80:
        return _SELL, val
    return _NEUTRAL, val


def _vote_cci(high: pd.Series, low: pd.Series, close: pd.Series) -> tuple[str, float | None]:
    """CCI(20): <-100 BUY, >100 SELL, else NEUTRAL."""
    cci_series = ta.cci(high, low, close, length=20)
    val = _safe_float(cci_series)
    if val is None:
        return _NEUTRAL, None
    if val < -100:
        return _BUY, val
    if val > 100:
        return _SELL, val
    return _NEUTRAL, val


def _vote_macd(close: pd.Series) -> tuple[str, float | None, float | None]:
    """MACD(12,26,9): MACD line > signal = BUY, < signal = SELL."""
    macd_df = ta.macd(close, fast=12, slow=26, signal=9)
    if macd_df is None or macd_df.empty:
        return _NEUTRAL, None, None
    macd_col = [c for c in macd_df.columns if c.startswith("MACD_")]
    sig_col = [c for c in macd_df.columns if c.startswith("MACDs_")]
    if not macd_col or not sig_col:
        return _NEUTRAL, None, None
    m = _safe_float(macd_df[macd_col[0]])
    s = _safe_float(macd_df[sig_col[0]])
    if m is None or s is None:
        return _NEUTRAL, m, s
    if m > s:
        return _BUY, m, s
    if m < s:
        return _SELL, m, s
    return _NEUTRAL, m, s


def _vote_williams_r(high: pd.Series, low: pd.Series, close: pd.Series) -> tuple[str, float | None]:
    """Williams %R(14): <-80 BUY, >-20 SELL, else NEUTRAL."""
    wr_series = ta.willr(high, low, close, length=14)
    val = _safe_float(wr_series)
    if val is None:
        return _NEUTRAL, None
    if val < -80:
        return _BUY, val
    if val > -20:
        return _SELL, val
    return _NEUTRAL, val


def _vote_momentum(close: pd.Series) -> tuple[str, float | None]:
    """Momentum(10): >0 BUY, <0 SELL."""
    mom_series = ta.mom(close, length=10)
    val = _safe_float(mom_series)
    if val is None:
        return _NEUTRAL, None
    if val > 0:
        return _BUY, val
    if val < 0:
        return _SELL, val
    return _NEUTRAL, val


def _vote_adx(high: pd.Series, low: pd.Series, close: pd.Series) -> tuple[str, float | None]:
    """ADX(14): >25 and +DI > -DI = BUY; >25 and -DI > +DI = SELL; else NEUTRAL."""
    adx_df = ta.adx(high, low, close, length=14)
    if adx_df is None or adx_df.empty:
        return _NEUTRAL, None
    adx_col = [c for c in adx_df.columns if c.startswith("ADX_")]
    dmp_col = [c for c in adx_df.columns if c.startswith("DMP_")]
    dmn_col = [c for c in adx_df.columns if c.startswith("DMN_")]
    if not adx_col or not dmp_col or not dmn_col:
        return _NEUTRAL, None
    adx_val = _safe_float(adx_df[adx_col[0]])
    dmp = _safe_float(adx_df[dmp_col[0]])
    dmn = _safe_float(adx_df[dmn_col[0]])
    if adx_val is None or dmp is None or dmn is None:
        return _NEUTRAL, None
    if adx_val > 25 and dmp > dmn:
        return _BUY, adx_val
    if adx_val > 25 and dmn > dmp:
        return _SELL, adx_val
    return _NEUTRAL, adx_val


# ── Moving-average votes ──────────────────────────────────────────────────────

def _vote_ma(price: float, ma_val: float | None) -> str:
    """Price > MA = BUY, price < MA = SELL, else NEUTRAL."""
    if ma_val is None:
        return _NEUTRAL
    if price > ma_val:
        return _BUY
    if price < ma_val:
        return _SELL
    return _NEUTRAL


# ── Public function ───────────────────────────────────────────────────────────

def calculate_consensus(df: pd.DataFrame) -> TechnicalConsensus:
    """Compute technical consensus from OHLCV data without external API calls.

    Args:
        df: DataFrame with columns Open, High, Low, Close, Volume (daily bars).
            Must have at least 20 rows for meaningful results.

    Returns:
        TechnicalConsensus with oscillator/MA votes, overall rating, and raw values.
    """
    # Guard: empty or too-small dataframe
    _MIN_BARS = 2
    if df is None or len(df) < _MIN_BARS or "Close" not in df.columns:
        return TechnicalConsensus(
            source="local",
            overall=_NEUTRAL,
            oscillators=_NEUTRAL,
            moving_averages=_NEUTRAL,
            buy_count=0,
            neutral_count=0,
            sell_count=0,
            raw_indicators={},
        )

    # Ensure required columns exist with fallbacks
    close = df["Close"].astype(float)
    high = df.get("High", close).astype(float)
    low = df.get("Low", close).astype(float)
    current_price = float(close.iloc[-1])

    raw: dict[str, Any] = {"price": current_price}

    # ── Oscillator votes ──────────────────────────────────────────
    osc_votes: list[str] = []

    rsi_vote, rsi_val = _vote_rsi(close)
    osc_votes.append(rsi_vote)
    raw["RSI"] = rsi_val

    stoch_vote, stoch_val = _vote_stoch(high, low, close)
    osc_votes.append(stoch_vote)
    raw["Stoch.K"] = stoch_val

    cci_vote, cci_val = _vote_cci(high, low, close)
    osc_votes.append(cci_vote)
    raw["CCI"] = cci_val

    macd_vote, macd_val, macd_sig_val = _vote_macd(close)
    osc_votes.append(macd_vote)
    raw["MACD"] = macd_val
    raw["MACD.signal"] = macd_sig_val

    wr_vote, wr_val = _vote_williams_r(high, low, close)
    osc_votes.append(wr_vote)
    raw["W.R"] = wr_val

    mom_vote, mom_val = _vote_momentum(close)
    osc_votes.append(mom_vote)
    raw["Mom"] = mom_val

    adx_vote, adx_val = _vote_adx(high, low, close)
    osc_votes.append(adx_vote)
    raw["ADX"] = adx_val

    osc_label = _votes_to_label(osc_votes)

    # ── Moving-average votes ──────────────────────────────────────
    ma_votes: list[str] = []

    def _ema(length: int) -> float | None:
        s = ta.ema(close, length=length)
        return _safe_float(s)

    def _sma(length: int) -> float | None:
        s = ta.sma(close, length=length)
        return _safe_float(s)

    ma_specs = [
        ("EMA9",  _ema(9)),
        ("SMA20", _sma(20)),
        ("EMA21", _ema(21)),
        ("SMA50", _sma(50)),
        ("EMA50", _ema(50)),
        ("SMA100", _sma(100)),
        ("SMA200", _sma(200)),
    ]

    for name, val in ma_specs:
        raw[name] = val
        ma_votes.append(_vote_ma(current_price, val))

    ma_label = _votes_to_label(ma_votes)

    # ── Overall consensus ─────────────────────────────────────────
    osc_score = _label_to_score(osc_label)
    ma_score = _label_to_score(ma_label)
    overall_score = (osc_score + ma_score) / 2.0
    overall_label = _score_to_label(overall_score)

    # ── Vote counts (all votes combined) ─────────────────────────
    all_votes = osc_votes + ma_votes
    buy_count = sum(1 for v in all_votes if v == _BUY)
    neutral_count = sum(1 for v in all_votes if v == _NEUTRAL)
    sell_count = sum(1 for v in all_votes if v == _SELL)

    return TechnicalConsensus(
        source="local",
        overall=overall_label,
        oscillators=osc_label,
        moving_averages=ma_label,
        buy_count=buy_count,
        neutral_count=neutral_count,
        sell_count=sell_count,
        raw_indicators={k: v for k, v in raw.items() if v is not None},
    )
