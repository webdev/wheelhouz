"""Sample trade and signal fixtures for testing."""

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from src.models.analysis import SizedOpportunity, SmartStrike
from src.models.enums import SignalType
from src.models.signals import AlphaSignal


def make_alpha_signal(**overrides: Any) -> AlphaSignal:
    """Create an AlphaSignal with sensible defaults."""
    defaults: dict[str, Any] = {
        "symbol": "NVDA",
        "signal_type": SignalType.INTRADAY_DIP,
        "strength": 72.0,
        "direction": "sell_put",
        "reasoning": "NVDA down 3.2% today, IV rank 65. Sell into fear.",
        "expires": datetime.utcnow() + timedelta(hours=24),
    }
    defaults.update(overrides)
    return AlphaSignal(**defaults)


def make_smart_strike(**overrides: Any) -> SmartStrike:
    """Create a SmartStrike with sensible defaults."""
    defaults: dict[str, Any] = {
        "strike": Decimal("800.00"),
        "delta": -0.25,
        "premium": Decimal("12.50"),
        "yield_on_capital": 0.0156,
        "annualized_yield": 0.19,
        "technical_reason": "200 SMA support at $820, selling below for cushion",
        "strike_score": 82.0,
    }
    defaults.update(overrides)
    return SmartStrike(**defaults)


def make_sized_opportunity(**overrides: Any) -> SizedOpportunity:
    """Create a SizedOpportunity with sensible defaults."""
    defaults: dict[str, Any] = {
        "symbol": "NVDA",
        "trade_type": "sell_put",
        "strike": Decimal("800.00"),
        "expiration": date.today() + timedelta(days=30),
        "premium": Decimal("12.50"),
        "contracts": 2,
        "capital_deployed": Decimal("160000.00"),
        "portfolio_pct": 0.04,
        "yield_on_capital": 0.0156,
        "annualized_yield": 0.19,
        "conviction": "high",
        "signals": [make_alpha_signal()],
        "smart_strike": make_smart_strike(),
        "reasoning": "NVDA down 3.2% intraday, IV rank 65, at 200 SMA. "
                     "HIGH conviction: 2 confirming signals. "
                     "Sell 2x $800P at $12.50 for 19% annualized.",
    }
    defaults.update(overrides)
    return SizedOpportunity(**defaults)


# Pre-built fixtures
SAMPLE_DIP_SIGNAL = make_alpha_signal()

SAMPLE_IV_SIGNAL = make_alpha_signal(
    signal_type=SignalType.IV_RANK_SPIKE,
    strength=58.0,
    reasoning="NVDA IV rank spiked to 65 (+15 in 5 days). Premium is rich.",
)

SAMPLE_SUPPORT_SIGNAL = make_alpha_signal(
    signal_type=SignalType.SUPPORT_BOUNCE,
    strength=70.0,
    reasoning="NVDA at $875, within 3% of 200 SMA ($820). Sell puts at/below support.",
)

SAMPLE_TRADE = make_sized_opportunity()

SAMPLE_LOW_CONVICTION_TRADE = make_sized_opportunity(
    symbol="CRM",
    strike=Decimal("250.00"),
    premium=Decimal("4.20"),
    contracts=1,
    capital_deployed=Decimal("25000.00"),
    portfolio_pct=0.01,
    yield_on_capital=0.0168,
    annualized_yield=0.20,
    conviction="low",
    signals=[make_alpha_signal(
        symbol="CRM",
        signal_type=SignalType.IV_RANK_SPIKE,
        strength=42.0,
    )],
)
