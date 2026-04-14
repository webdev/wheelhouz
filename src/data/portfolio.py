"""Portfolio loading — convert broker positions to shared Position model.

Loads positions from:
1. YAML file (manual snapshot — bridge until E*Trade API)
2. Alpaca (paper trading)
3. E*Trade (live — TODO)
"""
from __future__ import annotations

import os
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import structlog
import yaml

from src.execution.alpaca_client import AlpacaPaperClient, AlpacaPosition
from src.models.position import PortfolioState, Position

logger = structlog.get_logger()

PORTFOLIO_YAML = Path(__file__).parent.parent.parent / "config" / "portfolio.yaml"


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


def load_portfolio_from_yaml(path: Path | None = None) -> PortfolioState:
    """Load portfolio from manual YAML snapshot.

    Bridge solution until E*Trade API is wired. Reads config/portfolio.yaml
    and returns Position objects for all options positions.
    Stock positions are used for NLV estimation and concentration.
    """
    yaml_path = path or PORTFOLIO_YAML
    if not yaml_path.exists():
        logger.info("no_portfolio_yaml")
        return PortfolioState()

    with open(yaml_path) as f:
        data = yaml.safe_load(f)

    if not data:
        return PortfolioState()

    positions: list[Position] = []

    # Options positions — these get position review
    for opt in data.get("options", []):
        exp = date.fromisoformat(opt["expiration"])
        days_to_expiry = max(0, (exp - date.today()).days)
        opt_type = opt["type"]
        qty = abs(opt["quantity"])
        position_type = f"short_{opt_type}" if opt["quantity"] < 0 else f"long_{opt_type}"

        positions.append(Position(
            symbol=opt["symbol"],
            position_type=position_type,
            quantity=qty,
            strike=Decimal(str(opt["strike"])),
            expiration=exp,
            entry_price=Decimal(str(opt["entry_price"])),
            current_price=Decimal(str(opt["current_price"])),
            underlying_price=Decimal("0"),  # filled by caller with market data
            cost_basis=Decimal(str(opt["strike"])) * 100,
            delta=0.0, theta=0.0, gamma=0.0, vega=0.0, iv=0.0,
            days_to_expiry=days_to_expiry,
            option_type=opt_type,
        ))

    # Estimate NLV from stock positions
    total_stock_value = Decimal("0")
    concentration: dict[str, float] = {}
    for stk in data.get("stocks", []):
        # Use cost basis as proxy — real price comes from market data
        value = Decimal(str(stk["cost_basis"])) * stk["quantity"]
        total_stock_value += value

    nlv = float(total_stock_value) if total_stock_value > 0 else 1_000_000.0

    # Build concentration from stock holdings
    for stk in data.get("stocks", []):
        value = float(Decimal(str(stk["cost_basis"])) * stk["quantity"])
        concentration[stk["symbol"]] = value / nlv

    logger.info("portfolio_loaded_yaml",
                options=len(positions),
                stocks=len(data.get("stocks", [])),
                estimated_nlv=round(nlv))

    return PortfolioState(
        positions=positions,
        net_liquidation=Decimal(str(round(nlv))),
        concentration=concentration,
    )


def load_portfolio_state() -> PortfolioState:
    """Load current portfolio — E*Trade live first, then YAML, then Alpaca fallback."""
    # 1. Try live E*Trade API
    try:
        from src.data.auth import get_session
        from src.data.broker import fetch_portfolio
        session = get_session()
        state = fetch_portfolio(session)
        if state.positions:
            # Build concentration from live data
            nlv = float(state.net_liquidation) if state.net_liquidation > 0 else 1.0
            for pos in state.positions:
                value = float(pos.market_value) if pos.market_value else 0.0
                state.concentration[pos.symbol] = (
                    state.concentration.get(pos.symbol, 0.0) + abs(value) / nlv
                )
            logger.info("portfolio_loaded_etrade", positions=len(state.positions),
                        nlv=str(state.net_liquidation))
            return state
    except Exception as e:
        logger.info("etrade_unavailable_falling_back", error=str(e))

    # 2. Fall back to YAML snapshot
    if PORTFOLIO_YAML.exists():
        return load_portfolio_from_yaml()

    # 3. Fall back to Alpaca paper trading
    try:
        client = AlpacaPaperClient()
        account = client.get_account()
    except Exception as e:
        logger.warning("portfolio_load_failed", error=str(e))
        return PortfolioState()

    positions = [alpaca_position_to_position(p) for p in account.positions]

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
