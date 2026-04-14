"""Portfolio loading — convert broker positions to shared Position model.

Loads positions from Alpaca (paper) or E*Trade (live), converts to
the shared Position model, and builds PortfolioState.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import structlog

from src.execution.alpaca_client import AlpacaPaperClient, AlpacaPosition
from src.models.position import PortfolioState, Position

logger = structlog.get_logger()


def alpaca_position_to_position(ap: AlpacaPosition) -> Position:
    """Convert AlpacaPosition to shared Position model.

    Parses OCC option symbol (e.g. PLTR260515P00125000) to extract
    underlying, expiration, option type, and strike.
    """
    underlying, expiration, option_type, strike = _parse_occ_symbol(ap.symbol)
    days_to_expiry = (expiration - date.today()).days if expiration else 0
    position_type = f"short_{option_type}" if ap.quantity < 0 else f"long_{option_type}"

    return Position(
        symbol=underlying,
        position_type=position_type,
        quantity=abs(ap.quantity),
        strike=strike,
        expiration=expiration,
        entry_price=ap.avg_entry_price,
        current_price=ap.current_price,
        underlying_price=Decimal("0"),  # filled by caller with market data
        cost_basis=strike * 100 if option_type == "put" else Decimal("0"),
        delta=0.0,  # filled from chain data when available
        theta=0.0,
        gamma=0.0,
        vega=0.0,
        iv=0.0,
        days_to_expiry=max(0, days_to_expiry),
        unrealized_pnl=ap.unrealized_pnl,
        market_value=ap.market_value,
        option_type=option_type,
    )


def load_portfolio_state() -> PortfolioState:
    """Load current portfolio from Alpaca and convert to PortfolioState."""
    try:
        client = AlpacaPaperClient()
        account = client.get_account()
    except Exception as e:
        logger.warning("portfolio_load_failed", error=str(e))
        return PortfolioState()

    positions = [alpaca_position_to_position(p) for p in account.positions]

    # Build concentration map: exposure per underlying as % of NLV
    nlv = float(account.equity) if account.equity > 0 else 1.0
    concentration: dict[str, float] = {}
    for pos in positions:
        capital = float(pos.strike) * 100 * pos.quantity
        concentration[pos.symbol] = concentration.get(pos.symbol, 0.0) + capital / nlv

    return PortfolioState(
        positions=positions,
        cash_available=account.cash,
        buying_power=account.buying_power,
        net_liquidation=account.equity,
        concentration=concentration,
    )


def _parse_occ_symbol(occ: str) -> tuple[str, date | None, str, Decimal]:
    """Parse OCC option symbol: PLTR260515P00125000 → (PLTR, 2026-05-15, put, 125.00)."""
    match = re.match(r"^([A-Z]+)(\d{6})([PC])(\d{8})$", occ)
    if not match:
        # Not an option symbol — treat as stock
        return occ, None, "stock", Decimal("0")

    underlying = match.group(1)
    date_str = match.group(2)
    opt_type = "put" if match.group(3) == "P" else "call"
    strike_raw = int(match.group(4))
    strike = Decimal(str(strike_raw)) / 1000

    exp = date(2000 + int(date_str[:2]), int(date_str[2:4]), int(date_str[4:6]))
    return underlying, exp, opt_type, strike
