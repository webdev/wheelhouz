"""Portfolio loading — always fresh from E*Trade. No caching, no stale fallbacks."""

from __future__ import annotations

from decimal import Decimal

import structlog

from src.models.position import PortfolioState

logger = structlog.get_logger()


def load_portfolio_state() -> PortfolioState:
    """Load current portfolio — always fresh from E*Trade.

    If E*Trade is unavailable, returns empty state with a loud error rather
    than silently using stale data that causes phantom positions.
    """
    from src.data.auth import get_session
    from src.data.broker import fetch_portfolio
    try:
        session = get_session()
        state = fetch_portfolio(session)
    except Exception as e:
        logger.error("portfolio_load_failed_no_fallback",
                     error=str(e),
                     msg="E*Trade unavailable — briefing will run without portfolio data. "
                         "Fix auth before relying on position reviews.")
        return PortfolioState()

    if not state.positions:
        logger.warning("portfolio_empty", msg="E*Trade returned no positions")
        return state

    nlv = float(state.net_liquidation) if state.net_liquidation > 0 else 1.0
    for pos in state.positions:
        value = float(pos.market_value) if pos.market_value else 0.0
        state.concentration[pos.symbol] = (
            state.concentration.get(pos.symbol, 0.0) + abs(value) / nlv
        )
    logger.info("portfolio_loaded_etrade", positions=len(state.positions),
                nlv=str(state.net_liquidation))
    return state
