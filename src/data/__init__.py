"""Data pipeline — broker API, market data, events.

Public interface for the data collection layer.
"""

from src.data.auth import ETradeSession, authenticate_interactive, get_session
from src.data.broker import (
    fetch_accounts,
    fetch_option_chain,
    fetch_portfolio,
    fetch_quotes,
)
from src.data.events import fetch_event_calendar
from src.data.market import (
    calculate_iv_rank,
    fetch_market_context,
    fetch_price_history,
)

__all__ = [
    # Auth
    "ETradeSession",
    "authenticate_interactive",
    "get_session",
    # Broker
    "fetch_accounts",
    "fetch_portfolio",
    "fetch_quotes",
    "fetch_option_chain",
    # Market data
    "calculate_iv_rank",
    "fetch_price_history",
    "fetch_market_context",
    # Events
    "fetch_event_calendar",
]
