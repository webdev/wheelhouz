"""Portfolio loading — always fresh from E*Trade. No caching, no stale fallbacks."""

from __future__ import annotations

import subprocess
import sys

import structlog

from src.models.position import PortfolioState

logger = structlog.get_logger()


def load_portfolio_state() -> PortfolioState:
    """Load current portfolio — always fresh from E*Trade.

    If tokens are expired, auto-launches the auth flow (python -m src.data.auth --live).
    Portfolio data is essential — we never proceed with stale data.
    """
    from src.data.auth import get_session
    from src.data.broker import fetch_portfolio

    try:
        session = get_session()
    except RuntimeError as e:
        error_msg = str(e)
        if "expired" in error_msg.lower() or "token" in error_msg.lower():
            logger.warning("etrade_tokens_expired_auto_reauth",
                           msg="Launching auth flow to refresh tokens...")
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "src.data.auth", "--live"],
                    timeout=120,
                )
                if result.returncode == 0:
                    session = get_session()
                else:
                    logger.error("etrade_reauth_failed",
                                 msg="Auth flow exited with error. Run manually: "
                                     "python -m src.data.auth --live")
                    raise
            except subprocess.TimeoutExpired:
                logger.error("etrade_reauth_timeout",
                             msg="Auth flow timed out. Run manually: "
                                 "python -m src.data.auth --live")
                raise RuntimeError("E*Trade auth timed out") from e
        else:
            raise

    state = fetch_portfolio(session)
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
