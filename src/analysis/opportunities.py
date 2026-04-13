"""Full opportunity pipeline: signals -> strikes -> sizing -> ranking.

This is the main entry point for the analysis engine.
"""

from __future__ import annotations

from src.models.analysis import SizedOpportunity
from src.models.market import EventCalendar, MarketContext, OptionsChain, PriceHistory
from src.models.position import PortfolioState
from src.models.signals import AlphaSignal

from src.analysis.signals import detect_all_signals
from src.analysis.strikes import find_smart_strikes
from src.analysis.sizing import size_position
from src.config.loader import load_trading_params


def find_and_rank_opportunities(
    watchlist: list[str],
    market_data: dict[str, MarketContext],
    price_histories: dict[str, PriceHistory],
    option_chains: dict[str, OptionsChain],
    event_calendars: dict[str, EventCalendar],
    portfolio: PortfolioState,
) -> list[SizedOpportunity]:
    """Run the full pipeline for every symbol on the watchlist.

    Returns opportunities ranked by composite score (best first).
    Skips symbols with no signals, low IV, or earnings conflicts.
    """
    params = load_trading_params()
    wheel = params.get("wheel", {})
    min_iv_rank = float(wheel.get("min_iv_rank", 25))
    max_dte = int(wheel.get("max_dte", 45))
    sweet_spot_dte = int(wheel.get("sweet_spot_dte", 30))

    all_opportunities: list[SizedOpportunity] = []

    for symbol in watchlist:
        mkt = market_data.get(symbol)
        hist = price_histories.get(symbol)
        chain = option_chains.get(symbol)
        cal = event_calendars.get(symbol)

        if not mkt or not hist or not chain or not cal:
            continue

        # Detect all signals
        signals = detect_all_signals(symbol, mkt, hist, chain, cal)

        # No signals AND IV rank below threshold → skip
        if not signals and mkt.iv_rank < min_iv_rank:
            continue

        # Earnings within expiry window → skip (unless IV crush signal)
        from datetime import date
        if cal.next_earnings:
            days_to_er = (cal.next_earnings - date.today()).days
            has_crush = any(
                s.signal_type.value == "iv_crush_setup" for s in signals
            )
            if 0 < days_to_er < max_dte and not has_crush:
                continue

        # Find best strikes
        direction = "sell_put"
        smart_strikes = find_smart_strikes(
            symbol, chain, hist, direction, target_dte=sweet_spot_dte,
        )

        if not smart_strikes:
            continue

        best_strike = smart_strikes[0]

        # Get expiration from chain
        expiration = chain.get_expiry_near_dte(sweet_spot_dte)

        # Size the position
        sized = size_position(
            symbol=symbol,
            trade_type=direction,
            strike=best_strike,
            expiration=expiration,
            signals=signals,
            portfolio=portfolio,
        )
        all_opportunities.append(sized)

    # Rank by composite score
    all_opportunities.sort(key=_composite_score, reverse=True)
    return all_opportunities


def _composite_score(opp: SizedOpportunity) -> float:
    """Composite ranking: conviction * annualized yield * signal strength."""
    conviction_mult = {"high": 3.0, "medium": 1.5, "low": 1.0}.get(
        opp.conviction, 1.0
    )
    if opp.signals:
        signal_avg = sum(s.strength for s in opp.signals) / len(opp.signals)
    else:
        signal_avg = 30.0
    return opp.annualized_yield * conviction_mult * (signal_avg / 50.0)
