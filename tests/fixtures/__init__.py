"""Test fixtures for Wheel Copilot.

Provides realistic sample data for all shared models.
"""

from tests.fixtures.intelligence import (
    make_intelligence_context,
    make_portfolio_context,
    make_quant_intelligence,
    make_technical_consensus,
)
from tests.fixtures.market_data import (
    make_event_calendar,
    make_market_context,
    make_options_chain,
    make_price_history,
)
from tests.fixtures.sample_portfolio import (
    make_portfolio_state,
    make_position,
)
from tests.fixtures.trades import (
    make_alpha_signal,
    make_sized_opportunity,
    make_smart_strike,
)

__all__ = [
    "make_position",
    "make_portfolio_state",
    "make_market_context",
    "make_price_history",
    "make_event_calendar",
    "make_options_chain",
    "make_alpha_signal",
    "make_smart_strike",
    "make_sized_opportunity",
    "make_quant_intelligence",
    "make_technical_consensus",
    "make_portfolio_context",
    "make_intelligence_context",
]
