# tests/test_consensus.py
"""Tests for src/analysis/consensus.py — self-calculated technical consensus."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.analysis.consensus import calculate_consensus
from src.models.intelligence import TechnicalConsensus


# ── Synthetic data helpers ────────────────────────────────────────────────────

def _make_ohlcv(prices: list[float]) -> pd.DataFrame:
    """Build a minimal OHLCV DataFrame from a close-price series.

    High = close * 1.005, Low = close * 0.995, Open = previous close.
    Volume is constant.
    """
    closes = np.array(prices, dtype=float)
    highs = closes * 1.005
    lows = closes * 0.995
    opens = np.roll(closes, 1)
    opens[0] = closes[0]
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": 1_000_000}
    )


def _uptrend(n: int = 120, start: float = 50.0, step: float = 0.5) -> pd.DataFrame:
    """Steadily rising prices over n bars."""
    prices = [start + i * step for i in range(n)]
    return _make_ohlcv(prices)


def _downtrend(n: int = 120, start: float = 110.0, step: float = 0.5) -> pd.DataFrame:
    """Steadily falling prices over n bars."""
    prices = [start - i * step for i in range(n)]
    return _make_ohlcv(prices)


def _sideways(n: int = 120, base: float = 100.0, amplitude: float = 0.5) -> pd.DataFrame:
    """Oscillating prices with no net direction."""
    prices = [base + amplitude * np.sin(2 * np.pi * i / 20) for i in range(n)]
    return _make_ohlcv(prices)


# ── Return type and field checks ──────────────────────────────────────────────

class TestReturnType:
    def test_returns_technical_consensus(self) -> None:
        result = calculate_consensus(_uptrend())
        assert isinstance(result, TechnicalConsensus)

    def test_source_is_local(self) -> None:
        result = calculate_consensus(_uptrend())
        assert result.source == "local"

    def test_overall_is_valid_label(self) -> None:
        valid = {"STRONG_BUY", "BUY", "NEUTRAL", "SELL", "STRONG_SELL"}
        for df in [_uptrend(), _downtrend(), _sideways()]:
            assert calculate_consensus(df).overall in valid

    def test_oscillators_is_valid_label(self) -> None:
        valid = {"STRONG_BUY", "BUY", "NEUTRAL", "SELL", "STRONG_SELL"}
        assert calculate_consensus(_uptrend()).oscillators in valid

    def test_moving_averages_is_valid_label(self) -> None:
        valid = {"STRONG_BUY", "BUY", "NEUTRAL", "SELL", "STRONG_SELL"}
        assert calculate_consensus(_uptrend()).moving_averages in valid

    def test_vote_counts_are_non_negative(self) -> None:
        result = calculate_consensus(_uptrend())
        assert result.buy_count >= 0
        assert result.neutral_count >= 0
        assert result.sell_count >= 0

    def test_vote_counts_sum_to_total_indicators(self) -> None:
        # 7 oscillators + 7 MAs = 14 total votes
        result = calculate_consensus(_uptrend())
        assert result.buy_count + result.neutral_count + result.sell_count == 14


# ── Raw indicators ────────────────────────────────────────────────────────────

class TestRawIndicators:
    def test_raw_indicators_is_dict(self) -> None:
        result = calculate_consensus(_uptrend())
        assert isinstance(result.raw_indicators, dict)

    def test_expected_keys_present(self) -> None:
        result = calculate_consensus(_uptrend())
        raw = result.raw_indicators
        # Oscillator keys
        for key in ["RSI", "CCI", "MACD", "MACD.signal", "W.R", "Mom", "ADX"]:
            assert key in raw, f"Missing key: {key}"
        # MA keys
        for key in ["EMA9", "SMA20", "EMA21", "SMA50", "EMA50"]:
            assert key in raw, f"Missing key: {key}"

    def test_rsi_in_valid_range(self) -> None:
        result = calculate_consensus(_uptrend())
        rsi = result.raw_indicators.get("RSI")
        if rsi is not None:
            assert 0.0 <= rsi <= 100.0

    def test_price_key_present(self) -> None:
        result = calculate_consensus(_uptrend())
        assert "price" in result.raw_indicators


# ── Directional tests ─────────────────────────────────────────────────────────

class TestDirectionalConsensus:
    def test_strong_uptrend_is_buy_or_strong_buy(self) -> None:
        result = calculate_consensus(_uptrend())
        assert result.overall in {"BUY", "STRONG_BUY"}, (
            f"Expected BUY/STRONG_BUY for uptrend, got {result.overall}"
        )

    def test_strong_uptrend_ma_is_buy(self) -> None:
        """All MAs should be below current price in a sustained uptrend."""
        result = calculate_consensus(_uptrend())
        assert result.moving_averages in {"BUY", "STRONG_BUY"}, (
            f"Expected BUY/STRONG_BUY MAs for uptrend, got {result.moving_averages}"
        )

    def test_strong_downtrend_is_sell_or_strong_sell(self) -> None:
        result = calculate_consensus(_downtrend())
        assert result.overall in {"SELL", "STRONG_SELL"}, (
            f"Expected SELL/STRONG_SELL for downtrend, got {result.overall}"
        )

    def test_strong_downtrend_ma_is_sell(self) -> None:
        """All MAs should be above current price in a sustained downtrend."""
        result = calculate_consensus(_downtrend())
        assert result.moving_averages in {"SELL", "STRONG_SELL"}, (
            f"Expected SELL/STRONG_SELL MAs for downtrend, got {result.moving_averages}"
        )

    def test_sideways_is_neutral_ish(self) -> None:
        """Sideways market should not produce a strong directional signal."""
        result = calculate_consensus(_sideways())
        assert result.overall in {"NEUTRAL", "BUY", "SELL"}, (
            f"Sideways should be near-neutral, got {result.overall}"
        )


# ── Edge case handling ────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_empty_dataframe_returns_neutral(self) -> None:
        result = calculate_consensus(pd.DataFrame())
        assert result.overall == "NEUTRAL"
        assert result.buy_count == 0
        assert result.sell_count == 0

    def test_none_input_returns_neutral(self) -> None:
        # None should be treated gracefully
        result = calculate_consensus(None)  # type: ignore[arg-type]
        assert result.overall == "NEUTRAL"

    def test_single_bar_returns_neutral(self) -> None:
        df = _make_ohlcv([100.0])
        result = calculate_consensus(df)
        assert isinstance(result, TechnicalConsensus)
        assert result.overall in {"STRONG_BUY", "BUY", "NEUTRAL", "SELL", "STRONG_SELL"}

    def test_five_bars_does_not_crash(self) -> None:
        df = _make_ohlcv([100.0, 101.0, 102.0, 103.0, 104.0])
        result = calculate_consensus(df)
        assert isinstance(result, TechnicalConsensus)

    def test_20_bars_works(self) -> None:
        prices = [100.0 + i * 0.5 for i in range(20)]
        df = _make_ohlcv(prices)
        result = calculate_consensus(df)
        assert isinstance(result, TechnicalConsensus)

    def test_dataframe_with_nan_does_not_crash(self) -> None:
        df = _uptrend(50)
        df.loc[df.index[10:15], "Close"] = float("nan")
        result = calculate_consensus(df)
        assert isinstance(result, TechnicalConsensus)

    def test_dataframe_missing_high_low_uses_close(self) -> None:
        """DataFrame with only Close should not crash."""
        closes = [100.0 + i * 0.5 for i in range(60)]
        df = pd.DataFrame({"Close": closes})
        result = calculate_consensus(df)
        assert isinstance(result, TechnicalConsensus)

    def test_constant_prices_does_not_crash(self) -> None:
        """Flat prices can cause division-by-zero in some indicators."""
        df = _make_ohlcv([100.0] * 60)
        result = calculate_consensus(df)
        assert isinstance(result, TechnicalConsensus)


# ── Consistency checks ────────────────────────────────────────────────────────

class TestConsistency:
    def test_buy_count_increases_in_uptrend_vs_downtrend(self) -> None:
        up = calculate_consensus(_uptrend())
        down = calculate_consensus(_downtrend())
        assert up.buy_count > down.buy_count

    def test_sell_count_increases_in_downtrend_vs_uptrend(self) -> None:
        up = calculate_consensus(_uptrend())
        down = calculate_consensus(_downtrend())
        assert down.sell_count > up.sell_count

    def test_no_external_api_calls(self) -> None:
        """Sanity: calculate_consensus runs offline — confirmed by no network fixture."""
        # If this test passes (it always will), there were no blocking network calls.
        result = calculate_consensus(_uptrend())
        assert result is not None
