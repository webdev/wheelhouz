"""Execution layer — paper trading, gate validation, order management, Alpaca."""

from src.execution.alpaca_client import (
    AlpacaAccountInfo,
    AlpacaConfig,
    AlpacaOrder,
    AlpacaPaperClient,
    AlpacaPosition,
    build_option_symbol,
)
from src.execution.gate import validate_gate
from src.execution.orders import (
    calculate_smart_limit,
    estimate_fill_cost,
    is_in_trading_window,
    is_spread_acceptable,
)
from src.execution.paper_trader import PaperTrader

__all__ = [
    "AlpacaAccountInfo",
    "AlpacaConfig",
    "AlpacaOrder",
    "AlpacaPaperClient",
    "AlpacaPosition",
    "build_option_symbol",
    "PaperTrader",
    "validate_gate",
    "calculate_smart_limit",
    "is_spread_acceptable",
    "is_in_trading_window",
    "estimate_fill_cost",
]
