"""Sample market data fixtures for testing."""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from src.models.market import EventCalendar, MarketContext, OptionContract, OptionsChain, PriceHistory


def make_market_context(**overrides: Any) -> MarketContext:
    """Create a MarketContext with sensible defaults."""
    defaults: dict[str, Any] = {
        "symbol": "NVDA",
        "iv_rank": 65.0,
        "iv_percentile": 72.0,
        "iv_rank_change_5d": 15.0,
        "iv_30d": 0.48,
        "hv_30d": 0.38,
        "iv_hv_spread": 0.10,
        "price": Decimal("875.00"),
        "price_change_1d": -2.8,
        "price_change_5d": -5.2,
        "price_vs_52w_high": -12.0,
        "price_vs_200sma": 8.5,
        "put_call_ratio": 1.2,
        "option_volume_vs_avg": 1.8,
        "vix": 22.5,
        "vix_change_1d": 1.8,
        "vix_term_structure": "backwardation",
    }
    defaults.update(overrides)
    return MarketContext(**defaults)


def make_price_history(**overrides: Any) -> PriceHistory:
    """Create a PriceHistory with sensible defaults."""
    # Generate 252 days of synthetic closes trending up with noise
    base = 800.0
    closes = [Decimal(str(round(base + i * 0.3 + (i % 7 - 3) * 2, 2)))
              for i in range(252)]
    volumes = [float(50_000_000 + (i % 10) * 5_000_000) for i in range(252)]

    defaults: dict[str, Any] = {
        "symbol": "NVDA",
        "current_price": Decimal("875.00"),
        "sma_200": Decimal("820.00"),
        "sma_50": Decimal("860.00"),
        "sma_20": Decimal("870.00"),
        "ema_9": Decimal("872.00"),
        "high_52w": Decimal("995.00"),
        "low_52w": Decimal("680.00"),
        "recent_swing_high": Decimal("910.00"),
        "recent_swing_low": Decimal("850.00"),
        "anchored_vwap_90d": Decimal("855.00"),
        "rsi_14": 35.0,
        "daily_closes": closes,
        "daily_volumes": volumes,
    }
    defaults.update(overrides)
    return PriceHistory(**defaults)


def make_event_calendar(**overrides: Any) -> EventCalendar:
    """Create an EventCalendar with sensible defaults."""
    defaults: dict[str, Any] = {
        "symbol": "NVDA",
        "next_earnings": date.today() + timedelta(days=45),
        "earnings_confirmed": False,
        "next_ex_dividend": date.today() + timedelta(days=60),
        "dividend_amount": Decimal("0.04"),
        "fed_meeting": date.today() + timedelta(days=20),
        "fed_speakers_today": [],
        "cpi_ppi_date": date.today() + timedelta(days=10),
        "major_macro_event": None,
    }
    defaults.update(overrides)
    return EventCalendar(**defaults)


def make_options_chain(**overrides: Any) -> OptionsChain:
    """Create an OptionsChain with sensible defaults."""
    defaults: dict[str, Any] = {
        "symbol": "NVDA",
        "puts": [],
        "calls": [],
        "atm_iv": 0.30,
        "historical_skew_25d": 0.05,
        "iv_by_expiry": {"front_month": 0.45, "second_month": 0.40},
        "expirations": [],
    }
    defaults.update(overrides)
    return OptionsChain(**defaults)


# Pre-built fixtures
SAMPLE_MARKET_CONTEXT = make_market_context()
SAMPLE_PRICE_HISTORY = make_price_history()
SAMPLE_EVENT_CALENDAR = make_event_calendar()
SAMPLE_OPTIONS_CHAIN = make_options_chain()

# High-IV scenario
HIGH_IV_CONTEXT = make_market_context(
    iv_rank=85.0,
    iv_percentile=90.0,
    iv_30d=0.65,
    vix=32.0,
    vix_change_1d=5.0,
)

# Dip scenario
DIP_CONTEXT = make_market_context(
    price_change_1d=-4.5,
    price_change_5d=-8.0,
    vix=28.0,
)
