"""Tests for src/data/ — broker, market, events.

All external API calls are mocked. Never hits live APIs in tests.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from src.data.auth import ETradeSession, _build_clients, save_tokens, load_tokens
from src.data.broker import (
    _parse_position,
    _decimal,
    _float,
    _parse_date,
    fetch_quotes,
)
from src.data.events import fetch_event_calendar, _next_fed_meeting
from src.data.market import _calculate_rsi, calculate_iv_rank, fetch_price_history


# ── Helpers ──────────────────────────────────────────────────────


class TestDecimalFloat:
    def test_decimal_from_number(self) -> None:
        assert _decimal(12.5) == Decimal("12.5")

    def test_decimal_from_none(self) -> None:
        assert _decimal(None) == Decimal("0")

    def test_decimal_from_string(self) -> None:
        assert _decimal("100.00") == Decimal("100.00")

    def test_float_from_number(self) -> None:
        assert _float(3.14) == 3.14

    def test_float_from_none(self) -> None:
        assert _float(None) == 0.0

    def test_float_from_garbage(self) -> None:
        assert _float("not_a_number") == 0.0


class TestParseDate:
    def test_valid_date(self) -> None:
        assert _parse_date(2026, 5, 15) == date(2026, 5, 15)

    def test_invalid_date(self) -> None:
        assert _parse_date(None, None, None) is None

    def test_string_date(self) -> None:
        assert _parse_date("2026", "5", "15") == date(2026, 5, 15)


# ── Position parsing ────────────────────────────────────────────


class TestParsePosition:
    def test_short_put(self) -> None:
        raw = {
            "Product": {
                "symbol": "NVDA",
                "securityType": "OPTN",
                "strikePrice": 800.0,
                "expiryYear": 2026,
                "expiryMonth": 5,
                "expiryDay": 15,
                "callPut": "PUT",
            },
            "quantity": -2,
            "totalCost": -2500.0,
            "marketValue": -1250.0,
            "pricePaid": 12.5,
            "Quick": {"lastTrade": 6.25},
        }
        pos = _parse_position(raw, "acct_001")
        assert pos is not None
        assert pos.symbol == "NVDA"
        assert pos.position_type == "short_put"
        assert pos.quantity == 2
        assert pos.strike == Decimal("800.0")
        assert pos.option_type == "put"
        assert pos.account_id == "acct_001"

    def test_long_stock(self) -> None:
        raw = {
            "Product": {
                "symbol": "AAPL",
                "securityType": "EQ",
            },
            "quantity": 100,
            "totalCost": 17000.0,
            "marketValue": 18500.0,
            "pricePaid": 170.0,
            "Quick": {"lastTrade": 185.0},
        }
        pos = _parse_position(raw, "acct_002")
        assert pos is not None
        assert pos.position_type == "long_stock"
        assert pos.quantity == 100
        assert pos.strike == Decimal("0")
        assert pos.expiration is None

    def test_unknown_security_type_returns_none(self) -> None:
        raw = {
            "Product": {"symbol": "BOND", "securityType": "BOND"},
            "quantity": 10,
            "Quick": {},
        }
        assert _parse_position(raw, "acct") is None


# ── RSI ─────────────────────────────────────────────────────────


class TestRSI:
    def test_rsi_all_up(self) -> None:
        closes = list(range(100, 120))  # steady uptrend
        rsi = _calculate_rsi(closes)
        assert rsi is not None
        assert rsi == 100.0

    def test_rsi_all_down(self) -> None:
        closes = list(range(120, 100, -1))  # steady downtrend
        rsi = _calculate_rsi(closes)
        assert rsi is not None
        assert rsi == 0.0

    def test_rsi_insufficient_data(self) -> None:
        assert _calculate_rsi([100, 101, 102]) is None

    def test_rsi_mixed(self) -> None:
        # Alternating up/down
        closes = [100 + (i % 3) for i in range(30)]
        rsi = _calculate_rsi(closes)
        assert rsi is not None
        assert 0 <= rsi <= 100


# ── Events ───────────────────────────────────────────────────────


class TestEvents:
    def test_next_fed_meeting_returns_future_date(self) -> None:
        result = _next_fed_meeting()
        # Could be None if all 2026 dates passed, but during 2026 it won't be
        if result is not None:
            assert result >= date.today()

    @patch("src.data.events.yf.Ticker")
    def test_fetch_event_calendar_empty_ticker(self, mock_ticker_cls: MagicMock) -> None:
        mock_ticker = MagicMock()
        mock_ticker.calendar = None
        mock_ticker.dividends = None
        mock_ticker_cls.return_value = mock_ticker

        cal = fetch_event_calendar("FAKE")
        assert cal.symbol == "FAKE"
        assert cal.next_earnings is None


# ── Price History ────────────────────────────────────────────────


class TestPriceHistory:
    @patch("src.data.market.yf.Ticker")
    def test_fetch_price_history_empty(self, mock_ticker_cls: MagicMock) -> None:
        import pandas as pd

        mock_ticker = MagicMock()
        mock_ticker.history.return_value = pd.DataFrame()
        mock_ticker_cls.return_value = mock_ticker

        hist = fetch_price_history("FAKE")
        assert hist.symbol == "FAKE"
        assert hist.current_price == Decimal("0")

    @patch("src.data.market.yf.Ticker")
    def test_fetch_price_history_with_data(self, mock_ticker_cls: MagicMock) -> None:
        import pandas as pd
        import numpy as np

        dates = pd.date_range(end="2026-04-13", periods=252, freq="B")
        prices = np.linspace(800, 875, 252)
        volumes = np.full(252, 50_000_000.0)

        df = pd.DataFrame(
            {"Close": prices, "Volume": volumes},
            index=dates,
        )
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = df
        mock_ticker_cls.return_value = mock_ticker

        hist = fetch_price_history("NVDA")
        assert hist.symbol == "NVDA"
        assert hist.current_price > Decimal("0")
        assert hist.sma_200 is not None
        assert hist.sma_50 is not None
        assert hist.rsi_14 is not None
        assert len(hist.daily_closes) == 252


# ── Token persistence ───────────────────────────────────────────


class TestTokenPersistence:
    def test_save_and_load_tokens(self, tmp_path: object) -> None:
        import src.data.auth as auth_mod
        from pathlib import Path

        token_file = Path(str(tmp_path)) / "tokens.json"
        original_file = auth_mod.TOKEN_FILE

        try:
            auth_mod.TOKEN_FILE = token_file
            save_tokens("test_token", "test_secret", True)
            loaded = load_tokens()
            assert loaded is not None
            assert loaded["oauth_token"] == "test_token"
            assert loaded["oauth_secret"] == "test_secret"
            assert loaded["sandbox"] is True
        finally:
            auth_mod.TOKEN_FILE = original_file
