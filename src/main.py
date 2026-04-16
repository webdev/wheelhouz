"""Wheel Copilot orchestrator — wires all modules together.

Run modes:
  python -m src.main                    # Full daemon (5x daily + monitor)
  python -m src.main --mode briefing    # Single morning briefing
  python -m src.main --mode paper       # Paper trading (Alpaca)
  python -m src.main --mode backtest    # Run backtests
  python -m src.main --mode onboard     # First-time onboarding
  python -m src.main --mode weekend-review  # Saturday learning loop
"""

from __future__ import annotations

import argparse
import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, time, timezone
from decimal import Decimal
from typing import Any

import structlog

logger = structlog.get_logger()

from src.analysis.signals import detect_all_signals
from src.analysis.sizing import size_position
from src.analysis.strikes import find_smart_strikes
from src.config.loader import load_watchlist
from src.data.events import fetch_event_calendar
from src.data.market import fetch_market_context, fetch_options_chain, fetch_price_history
from src.models.analysis import SizedOpportunity
from src.models.market import EventCalendar, MarketContext, OptionsChain, PriceHistory
from src.models.position import PortfolioState
from src.models.signals import AlphaSignal
from src.monitor.regime import RegimeState, classify_regime
from src.risk import check_liquidity_health, generate_tax_alerts
from src.models.account import AccountRouter
from src.data.tradingview import fetch_tradingview_consensus
from src.intelligence.builder import build_intelligence_context
from src.intelligence.position_review import PositionReview, review_position, format_position_review
from src.data.portfolio import load_portfolio_state
from src.delivery.reasoning import generate_analyst_brief
from src.models.intelligence import IntelligenceContext
from src.models.shopping_list import BenchEntry, ShoppingListEntry
from src.data.shopping_list import fetch_shopping_list
from src.analysis.bench import build_bench
from src.models.market import OptionContract

log = structlog.get_logger()


@dataclass
class LeapCandidate:
    """A LEAP call candidate screened from the watchlist."""
    symbol: str
    price: float
    iv_rank: float
    rsi: float
    reasons: list[str]
    expiration: str | None = None     # "Dec 2028"
    dte: int | None = None
    strike: Decimal | None = None
    delta: float | None = None
    bid: float | None = None
    ask: float | None = None
    mid: float | None = None
    open_interest: int | None = None


# ---------------------------------------------------------------------------
# Analysis cycle definitions
# ---------------------------------------------------------------------------

ANALYSIS_CYCLES = [
    {
        "name": "morning",
        "time": time(8, 0),
        "always_push": True,
        "alert_conditions": ["always"],
    },
    {
        "name": "post_open",
        "time": time(10, 30),
        "always_push": False,
        "alert_conditions": [
            "new_dip_signal_fired",
            "morning_trade_invalidated",
            "regime_changed",
        ],
    },
    {
        "name": "midday",
        "time": time(13, 0),
        "always_push": False,
        "alert_conditions": [
            "portfolio_delta_outside_range",
            "concentration_violation_new",
            "new_high_conviction_signal",
        ],
    },
    {
        "name": "eod",
        "time": time(15, 30),
        "always_push": False,
        "alert_conditions": [
            "position_expiring_this_week_at_risk",
            "earnings_tonight_on_open_position",
            "close_winner_before_eod",
        ],
    },
    {
        "name": "post_market",
        "time": time(16, 30),
        "always_push": True,
        "alert_conditions": ["always"],
    },
]

SENTINEL_TIMES = [time(6, 0), time(7, 0), time(7, 30)]


# ---------------------------------------------------------------------------
# Live data fetchers
# ---------------------------------------------------------------------------

def fetch_vix_and_spy() -> tuple[float, float]:
    """Fetch current VIX level and SPY daily change from yfinance."""
    import yfinance as yf

    vix_val = 20.0
    spy_change = 0.0

    try:
        vix = yf.Ticker("^VIX")
        vix_hist = vix.history(period="2d")
        if not vix_hist.empty:
            vix_closes = [float(c) for c in vix_hist["Close"]]
            vix_val = vix_closes[-1]
    except Exception as e:
        log.warning("vix_fetch_failed", error=str(e))

    try:
        spy = yf.Ticker("SPY")
        spy_hist = spy.history(period="5d")
        if not spy_hist.empty and len(spy_hist) >= 2:
            spy_closes = [float(c) for c in spy_hist["Close"]]
            prev = spy_closes[-2]
            curr = spy_closes[-1]
            spy_change = (curr - prev) / prev if prev else 0.0
    except Exception as e:
        log.warning("spy_fetch_failed", error=str(e))

    return vix_val, spy_change


def fetch_all_watchlist_data(
    symbols: list[str],
    etrade_session: object | None = None,
) -> list[tuple[str, MarketContext, PriceHistory, OptionsChain, EventCalendar]]:
    """Fetch real market data for all watchlist symbols.

    Options chains: E*Trade first (real bid/ask/Greeks), yfinance fallback.
    """
    results = []
    for symbol in symbols:
        log.info("fetching_data", symbol=symbol)
        try:
            mkt = fetch_market_context(symbol)
            hist = fetch_price_history(symbol)

            # Options chain: E*Trade first (accurate), yfinance fallback
            chain = OptionsChain(symbol=symbol)
            if etrade_session:
                try:
                    from src.data.broker import fetch_etrade_chain
                    chain = fetch_etrade_chain(
                        etrade_session, symbol, float(hist.current_price),
                    )
                except Exception as e:
                    log.warning("etrade_chain_fallback", symbol=symbol, error=str(e))

            if not chain.puts and not chain.calls:
                chain = fetch_options_chain(symbol)

            cal = fetch_event_calendar(symbol)
            results.append((symbol, mkt, hist, chain, cal))
        except Exception as e:
            log.warning("symbol_fetch_failed", symbol=symbol, error=str(e))
    return results


# ---------------------------------------------------------------------------
# Wheel candidate scanner — screens broader universe for high-IV opportunities
# ---------------------------------------------------------------------------

@dataclass
class ScannerPick:
    """A wheel candidate found by the scanner."""
    symbol: str
    price: float
    iv_rank: float
    rsi: float | None
    put_contract: Any | None  # OptionContract
    score: float  # composite attractiveness score
    reasons: list[str]
    collateral_per_contract: float  # strike * 100
    ann_yield: float  # annualized premium yield
    market_cap: float = 0.0  # in dollars, for tier labeling
    next_earnings: Any = None  # date | None — for earnings gate
    shopping_list_rating: str | None = None    # e.g. "Buy", "Top 15 Stock"
    price_target: str | None = None            # e.g. "$500-550"


def scan_wheel_candidates(
    watchlist_symbols: set[str],
    etrade_session: object | None = None,
    max_picks: int = 8,
    shopping_list: list[ShoppingListEntry] | None = None,
) -> list[ScannerPick]:
    """Discover and screen wheel candidates dynamically.

    When shopping_list is provided, uses it as the primary discovery universe.
    Falls back to Finviz if shopping list yields < 3 picks after screening.
    """
    from src.data.scanner_sources import discover_scanner_universe
    from src.data.market import calculate_iv_rank
    from datetime import date

    candidates: list[str] = []
    sl_metadata: dict[str, Any] = {}  # ticker → ShoppingListEntry

    # Phase 1: Discover candidates
    if shopping_list:
        # Shopping list is primary universe
        scored: list[tuple[str, float, ShoppingListEntry]] = []
        for entry in shopping_list:
            if entry.ticker in watchlist_symbols:
                continue
            if entry.rating_tier == 0:  # Sell
                continue

            # Composite score: rating_tier * 3 + freshness_bonus
            freshness = 0.0
            if entry.date_updated:
                age = (date.today() - entry.date_updated).days
                if age <= 30:
                    freshness = 1.0
                elif age <= 60:
                    freshness = 0.5
            score = entry.rating_tier * 3 + freshness
            scored.append((entry.ticker, score, entry))

        scored.sort(key=lambda x: x[1], reverse=True)
        candidates = [s[0] for s in scored[:40]]
        sl_metadata = {s[0]: s[2] for s in scored}
        log.info("scanner_shopping_list_candidates", count=len(candidates))
    else:
        # Fallback: Finviz discovery
        candidates = discover_scanner_universe(watchlist_symbols, max_candidates=60)

    if not candidates:
        log.info("scanner_no_candidates")
        return []

    # Phase 2: Detailed screening on top candidates
    # Finviz already pre-sorted by volatility and pre-filtered RSI/price,
    # so we only need IV rank + chain data for the top N
    screen_limit = min(len(candidates), 25)
    log.info("scanner_screening", candidates=screen_limit, discovered=len(candidates))
    picks: list[ScannerPick] = []

    for symbol in candidates[:screen_limit]:
        try:
            # IV rank from yfinance historical vol
            iv_data = calculate_iv_rank(symbol, 0)  # 0 = use HV proxy
            iv_rank = iv_data["iv_rank"]

            # IV filter: need rich premium
            if iv_rank < 35:
                continue

            # Get current price + RSI from yfinance (Finviz data may be slightly stale)
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="3mo")
            if hist.empty or len(hist) < 20:
                continue

            closes = [float(c) for c in hist["Close"]]
            price = closes[-1]

            # Market cap + earnings date from yfinance
            mcap = 0.0
            next_earn = None
            try:
                info = ticker.info
                mcap = float(info.get("marketCap", 0) or 0)
                # Earnings date — yfinance returns as timestamp
                earn_ts = info.get("earningsTimestamp") or info.get("mostRecentQuarter")
                if not earn_ts:
                    # Try the calendar approach
                    earn_dates = ticker.get_earnings_dates(limit=4)
                    if earn_dates is not None and not earn_dates.empty:
                        from datetime import date as date_cls
                        future = [d.date() for d in earn_dates.index if d.date() > date_cls.today()]
                        if future:
                            next_earn = min(future)
            except Exception:
                pass

            from src.data.market import _calculate_rsi
            rsi = _calculate_rsi(closes)
            if rsi is not None and rsi > 65:
                continue

            # Score: IV rank + RSI pullback + price efficiency
            score = 0.0
            reasons: list[str] = []

            # IV attractiveness (main driver)
            if iv_rank >= 70:
                score += 4
                reasons.append(f"IV rank {iv_rank:.0f} — premium rich")
            elif iv_rank >= 55:
                score += 3
                reasons.append(f"IV rank {iv_rank:.0f} — elevated premium")
            elif iv_rank >= 40:
                score += 2
                reasons.append(f"IV rank {iv_rank:.0f}")
            else:
                score += 1
                reasons.append(f"IV rank {iv_rank:.0f}")

            # RSI pullback bonus
            if rsi is not None and rsi < 30:
                score += 3
                reasons.append(f"RSI {rsi:.0f} — oversold")
            elif rsi is not None and rsi <= 45:
                score += 2
                reasons.append(f"RSI {rsi:.0f} — pullback")

            # Affordable collateral bonus
            if price <= 25:
                score += 2
                reasons.append(f"${price:.0f} — low collateral per contract")
            elif price <= 50:
                score += 1

            # Try to get a put contract
            put_contract = None
            ann_yield = 0.0
            collateral = price * 100
            try:
                if etrade_session:
                    from src.data.broker import fetch_etrade_chain
                    chain = fetch_etrade_chain(etrade_session, symbol, price)
                else:
                    chain = fetch_options_chain(symbol)

                if chain and chain.puts:
                    from datetime import date as date_type
                    today = date_type.today()
                    # E*Trade provides real delta; yfinance has delta=0.0
                    has_delta = any(abs(p.delta) > 0.01 for p in chain.puts[:5])

                    def _valid_put(p: Any) -> bool:
                        dte = (p.expiration - today).days
                        if not (20 <= dte <= 55 and p.bid > 0):
                            return False
                        # Never recommend puts that expire after earnings
                        if next_earn and p.expiration >= next_earn:
                            return False
                        return True

                    if has_delta:
                        put_candidates = [
                            p for p in chain.puts
                            if 0.10 <= abs(p.delta) <= 0.35 and _valid_put(p)
                        ]
                    else:
                        # Fallback: select by strike distance (5-15% OTM)
                        put_candidates = [
                            p for p in chain.puts
                            if 0.85 <= float(p.strike) / price <= 0.95 and _valid_put(p)
                        ]
                    if put_candidates:
                        if has_delta:
                            put_contract = min(
                                put_candidates,
                                key=lambda p: (abs(abs(p.delta) - 0.25)
                                               + abs((p.expiration - today).days - 37) / 100)
                            )
                        else:
                            put_contract = min(
                                put_candidates,
                                key=lambda p: abs(float(p.strike) / price - 0.92)
                            )
                        strike_f = float(put_contract.strike)
                        mid = float(put_contract.mid)
                        dte = (put_contract.expiration - today).days
                        collateral = strike_f * 100
                        if strike_f > 0 and dte > 0:
                            yield_pct = (mid / strike_f) * 100
                            ann_yield = yield_pct * (365 / dte)
                            if ann_yield >= 20:
                                score += 2
                            elif ann_yield >= 10:
                                score += 1
            except Exception as e:
                log.debug("scanner_chain_failed", symbol=symbol, error=str(e))

            picks.append(ScannerPick(
                symbol=symbol,
                price=price,
                iv_rank=iv_rank,
                rsi=rsi,
                put_contract=put_contract,
                score=score,
                reasons=reasons,
                collateral_per_contract=collateral,
                ann_yield=ann_yield,
                market_cap=mcap,
                next_earnings=next_earn,
            ))

            # Attach shopping list metadata if available
            if picks and picks[-1].symbol in sl_metadata:
                sl_entry = sl_metadata[picks[-1].symbol]
                picks[-1].shopping_list_rating = sl_entry.rating
                if sl_entry.price_target_2026:
                    low, high = sl_entry.price_target_2026
                    picks[-1].price_target = f"${low:,.0f}-{high:,.0f}"

        except Exception as e:
            log.debug("scanner_symbol_failed", symbol=symbol, error=str(e))
            continue

    # Backfill from Finviz if shopping list yielded < 3 picks
    if shopping_list and len(picks) < 3:
        log.info("scanner_backfilling_finviz", shopping_list_picks=len(picks))
        existing_symbols = {p.symbol for p in picks}
        finviz_candidates = discover_scanner_universe(watchlist_symbols, max_candidates=60)
        for symbol in finviz_candidates:
            if symbol in existing_symbols or symbol in watchlist_symbols:
                continue
            try:
                iv_data = calculate_iv_rank(symbol, 0)
                iv_rank = iv_data["iv_rank"]
                if iv_rank < 35:
                    continue
                import yfinance as yf
                ticker = yf.Ticker(symbol)
                hist = ticker.history(period="3mo")
                if hist.empty or len(hist) < 20:
                    continue
                closes = [float(c) for c in hist["Close"]]
                price = closes[-1]
                from src.data.market import _calculate_rsi
                rsi = _calculate_rsi(closes)
                if rsi is not None and rsi > 65:
                    continue
                # Simplified scoring for backfill
                score = iv_rank / 20
                reasons = [f"IV rank {iv_rank:.0f}", "Scanner backfill"]
                picks.append(ScannerPick(
                    symbol=symbol, price=price, iv_rank=iv_rank, rsi=rsi,
                    put_contract=None, score=score, reasons=reasons,
                    collateral_per_contract=price * 100, ann_yield=0.0,
                ))
                existing_symbols.add(symbol)
                if len(picks) >= max_picks:
                    break
            except Exception as e:
                log.debug("scanner_backfill_failed", symbol=symbol, error=str(e))
                continue

    # Sort by score descending, then by annualized yield
    picks.sort(key=lambda p: (p.score, p.ann_yield), reverse=True)
    log.info("scanner_complete", picks=len(picks), screened=screen_limit)
    return picks[:max_picks]


# ---------------------------------------------------------------------------
# Recommendation engine — turns signals into sized trade proposals
# ---------------------------------------------------------------------------

def build_recommendations(
    all_signals: list[AlphaSignal],
    watchlist_data: list[tuple[str, MarketContext, PriceHistory, OptionsChain, EventCalendar]],
    portfolio: PortfolioState | None = None,
    intel_contexts: list[IntelligenceContext] | None = None,
    shopping_list: dict[str, ShoppingListEntry] | None = None,
) -> list[SizedOpportunity]:
    """Build trade recommendations: puts on dips, calls on strength.

    1. Signal-driven puts: quant dip signals fire → sell puts at support.
       TradingView consensus adjusts conviction (SELL downgrades, BUY upgrades).
    2. Covered calls: TV shows strength on owned stock → sell calls at resistance.

    Returns a list of SizedOpportunity sorted by conviction then yield.
    """
    from collections import defaultdict
    from datetime import date as date_type, timedelta

    if portfolio is None:
        portfolio = PortfolioState()  # NLV=0 → sizing falls back to $1M

    # Split signals by direction
    put_signals: dict[str, list[AlphaSignal]] = defaultdict(list)
    call_signals: dict[str, list[AlphaSignal]] = defaultdict(list)
    for s in all_signals:
        if s.direction == "sell_call":
            call_signals[s.symbol].append(s)
        else:
            put_signals[s.symbol].append(s)

    # Index TradingView consensus by symbol
    tv_by_symbol: dict[str, str] = {}
    if intel_contexts:
        for ctx in intel_contexts:
            if ctx.technical_consensus:
                tv_by_symbol[ctx.symbol] = ctx.technical_consensus.overall

    # Index owned stock for covered call eligibility (no naked calls)
    owned_shares: dict[str, int] = {}
    existing_short_calls: dict[str, int] = {}
    if portfolio:
        for pos in portfolio.positions:
            if pos.position_type == "long_stock" and pos.quantity >= 100:
                owned_shares[pos.symbol] = owned_shares.get(pos.symbol, 0) + pos.quantity
            elif pos.position_type == "short_call":
                existing_short_calls[pos.symbol] = (
                    existing_short_calls.get(pos.symbol, 0) + pos.quantity
                )

    price_data = {sym: (mkt, hist, chain) for sym, mkt, hist, chain, _ in watchlist_data}
    event_data = {sym: cal for sym, _, _, _, cal in watchlist_data}
    target_exp = date_type.today() + timedelta(days=30)

    recommendations: list[SizedOpportunity] = []
    sized_symbols: set[str] = set()

    # 1. Signal-driven PUT recommendations (quant dip signals fired)
    for symbol, sigs in put_signals.items():
        if symbol not in price_data:
            continue

        # Earnings gate: never sell through earnings unless earnings_crush
        cal = event_data.get(symbol)
        if cal and cal.next_earnings and cal.next_earnings <= target_exp:
            logger.info("rec_blocked_earnings", symbol=symbol, direction="sell_put",
                        earnings=str(cal.next_earnings))
            continue

        _, hist, chain = price_data[symbol]

        strikes = find_smart_strikes(symbol, chain, hist, "sell_put")
        if not strikes:
            continue

        sized = size_position(
            symbol=symbol,
            trade_type="sell_put",
            strike=strikes[0],
            expiration=target_exp,
            signals=sigs,
            portfolio=portfolio,
        )

        # TradingView conviction adjustment
        tv_overall = tv_by_symbol.get(symbol)
        if tv_overall:
            sized = _apply_tv_adjustment(sized, tv_overall)

        # Shopping list conviction adjustment
        if shopping_list:
            sized, _ = _apply_shopping_list_adjustment(sized, shopping_list)

        if sized.conviction != "skip" and sized.contracts > 0:
            recommendations.append(sized)
            sized_symbols.add(symbol)

    # 2. Signal-driven CALL recommendations (strength signals on owned stock)
    # RULE: only covered calls — must own 100+ shares of the underlying.
    for symbol, sigs in call_signals.items():
        if symbol not in price_data:
            continue
        shares = owned_shares.get(symbol, 0)
        if shares < 100:
            continue  # no naked calls

        # Earnings gate
        cal = event_data.get(symbol)
        if cal and cal.next_earnings and cal.next_earnings <= target_exp:
            logger.info("rec_blocked_earnings", symbol=symbol, direction="sell_call",
                        earnings=str(cal.next_earnings))
            continue

        max_contracts = (shares // 100) - existing_short_calls.get(symbol, 0)
        if max_contracts <= 0:
            continue

        _, hist, chain = price_data[symbol]
        strikes = find_smart_strikes(symbol, chain, hist, "sell_call")
        if not strikes:
            continue

        sized = size_position(
            symbol=symbol,
            trade_type="sell_call",
            strike=strikes[0],
            expiration=target_exp,
            signals=sigs,
            portfolio=portfolio,
        )

        # Cap contracts to what we can cover
        if sized.contracts > max_contracts:
            sized.contracts = max_contracts
            sized.capital_deployed = Decimal("0")  # covered calls don't tie up capital
            sized.portfolio_pct = 0.0

        # TV conviction adjustment for calls too
        tv_overall = tv_by_symbol.get(symbol)
        if tv_overall:
            sized = _apply_tv_adjustment(sized, tv_overall)

        # Shopping list conviction adjustment
        if shopping_list:
            sized, _ = _apply_shopping_list_adjustment(sized, shopping_list)

        if sized.conviction != "skip" and sized.contracts > 0:
            recommendations.append(sized)
            sized_symbols.add(symbol)

    # 3. TV-only covered call recommendations (no quant signals, but TV shows strength)
    # Catches cases where no call signal fired but TV says BUY on owned stock.
    if portfolio and intel_contexts:
        for symbol, shares in owned_shares.items():
            if symbol in sized_symbols or symbol not in price_data:
                continue

            # Earnings gate
            cal = event_data.get(symbol)
            if cal and cal.next_earnings and cal.next_earnings <= target_exp:
                continue

            max_contracts = (shares // 100) - existing_short_calls.get(symbol, 0)
            if max_contracts <= 0:
                continue

            ctx = next((c for c in intel_contexts if c.symbol == symbol), None)
            if not ctx or not ctx.technical_consensus:
                continue
            tc = ctx.technical_consensus
            if tc.overall not in ("BUY", "STRONG_BUY"):
                continue

            _, hist, chain = price_data[symbol]
            strikes = find_smart_strikes(symbol, chain, hist, "sell_call")
            if not strikes:
                continue

            best = strikes[0]
            recommendations.append(SizedOpportunity(
                symbol=symbol,
                trade_type="sell_call",
                strike=best.strike,
                expiration=target_exp,
                premium=best.premium,
                contracts=max_contracts,
                capital_deployed=Decimal("0"),
                portfolio_pct=0.0,
                yield_on_capital=best.yield_on_capital,
                annualized_yield=best.annualized_yield,
                conviction="low",
                signals=[],
                smart_strike=best,
                reasoning=(
                    f"TV {tc.overall} — sell calls into strength. "
                    f"{max_contracts}x ${best.strike}C on {shares} shares."
                ),
            ))
            # Shopping list conviction adjustment (TV-only calls have no TV adj step)
            if shopping_list:
                recommendations[-1], _ = _apply_shopping_list_adjustment(
                    recommendations[-1], shopping_list
                )
            sized_symbols.add(symbol)

    # Sort: high > medium > low, then by annualized yield descending
    conviction_rank = {"high": 0, "medium": 1, "low": 2}
    recommendations.sort(
        key=lambda r: (conviction_rank.get(r.conviction, 9), -r.annualized_yield),
    )
    return recommendations


_CONVICTION_LEVELS = ["skip", "low", "medium", "high"]


def _apply_tv_adjustment(sized: SizedOpportunity, tv_overall: str) -> SizedOpportunity:
    """Adjust conviction based on TradingView consensus.

    Bearish TV consensus vetoes or heavily penalizes trades:
    - STRONG_SELL → force SKIP (never trade against strong crowd consensus)
    - SELL → cap at LOW (watch list only — the crowd sees something)
    - BUY/STRONG_BUY → upgrade one level (crowd confirms thesis)
    - NEUTRAL → no change
    """
    current_idx = _CONVICTION_LEVELS.index(sized.conviction) if sized.conviction in _CONVICTION_LEVELS else 1
    original = sized.conviction

    if tv_overall == "STRONG_SELL":
        new_idx = 0  # skip
    elif tv_overall == "SELL":
        new_idx = min(current_idx, 1)  # cap at LOW
    elif tv_overall in ("BUY", "STRONG_BUY"):
        new_idx = min(len(_CONVICTION_LEVELS) - 1, current_idx + 1)
    else:
        return sized

    new_conviction = _CONVICTION_LEVELS[new_idx]
    if new_conviction == original:
        return sized

    direction = "downgraded" if new_idx < current_idx else "upgraded"
    tv_note = f" [TV {tv_overall} → {direction} from {original.upper()} to {new_conviction.upper()}]"
    sized.conviction = new_conviction
    sized.reasoning += tv_note
    return sized


def _apply_shopping_list_adjustment(
    sized: SizedOpportunity,
    shopping_list: dict[str, ShoppingListEntry],
) -> tuple[SizedOpportunity, str | None]:
    """Adjust conviction based on shopping list rating.

    Applied after TV adjustment. Stale entries (>90 days) are neutralized.
    Returns (sized_opportunity, label_or_none).
    """
    entry = shopping_list.get(sized.symbol)
    if not entry:
        return sized, None

    # Stale guard: no adjustment for old ratings
    if entry.stale:
        return sized, None

    current_idx = (
        _CONVICTION_LEVELS.index(sized.conviction)
        if sized.conviction in _CONVICTION_LEVELS
        else 1
    )
    original = sized.conviction

    tier = entry.rating_tier
    if tier == 5:
        new_idx = min(len(_CONVICTION_LEVELS) - 1, current_idx + 2)
    elif tier == 4:
        new_idx = min(len(_CONVICTION_LEVELS) - 1, current_idx + 1)
    elif tier in (3, 2):
        return sized, None  # Buy and Borderline Buy: no change
    elif tier == 1:
        new_idx = max(1, current_idx - 1)  # floor at low, not skip
    elif tier == 0:
        new_idx = max(1, current_idx - 1)  # floor at low, not skip
    else:
        return sized, None

    new_conviction = _CONVICTION_LEVELS[new_idx]
    if new_conviction == original:
        return sized, None

    sized.conviction = new_conviction

    # Generate label
    if tier == 5:
        label = "\u2B06 Upgraded (Top Stock \u2014 Parkev)"
    elif tier == 4:
        label = "\u2B06 Upgraded (Top 15 Stock \u2014 Parkev)"
    elif tier == 1:
        label = "\u2B07 Downgraded (Hold \u2014 Parkev)"
    elif tier == 0:
        label = "\u26A0 Sell-rated (Parkev)"
    else:
        label = None

    sized.conviction_label = label
    return sized, label


# ---------------------------------------------------------------------------
# Local briefing formatter (no Claude API needed)
# ---------------------------------------------------------------------------

# ANSI color helpers for terminal output
class _C:
    """ANSI escape codes for terminal color."""
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

    @staticmethod
    def red(s: str) -> str: return f"{_C.RED}{s}{_C.RESET}"
    @staticmethod
    def green(s: str) -> str: return f"{_C.GREEN}{s}{_C.RESET}"
    @staticmethod
    def yellow(s: str) -> str: return f"{_C.YELLOW}{s}{_C.RESET}"
    @staticmethod
    def blue(s: str) -> str: return f"{_C.BLUE}{s}{_C.RESET}"
    @staticmethod
    def magenta(s: str) -> str: return f"{_C.MAGENTA}{s}{_C.RESET}"
    @staticmethod
    def cyan(s: str) -> str: return f"{_C.CYAN}{s}{_C.RESET}"
    @staticmethod
    def dim(s: str) -> str: return f"{_C.DIM}{s}{_C.RESET}"
    @staticmethod
    def bold(s: str) -> str: return f"{_C.BOLD}{s}{_C.RESET}"


def _pnl_colored(pnl: Decimal, pnl_pct: float) -> str:
    """Color a P&L string: green for profit, red for loss."""
    if pnl >= 0:
        return _C.green(f"+${pnl:,.0f} ({pnl_pct:+.0%})")
    return _C.red(f"-${abs(pnl):,.0f} ({pnl_pct:+.0%})")


def _format_stress_value(value: Decimal) -> str:
    """Format a stress test value: negative = still profitable, positive = real loss."""
    if value <= 0:
        return f"safe (keep ${abs(value):,.0f})"
    return f"${value:,.0f} loss"


def _fmt_exp(d: "date") -> str:
    """Format expiration date — include year tick for dates beyond this calendar year."""
    from datetime import date as date_cls
    if d.year > date_cls.today().year:
        return d.strftime("%b %d '%y")
    return d.strftime("%b %d")


def _format_position_desc(p: PositionReview) -> str:
    """Format a position review into a readable description like 'GOOG -4x Dec 18 $380 call'."""
    if not p.option_type:
        return p.symbol
    qty_str = f"-{p.quantity}x" if p.quantity > 0 else f"{p.quantity}x"
    return f"{p.symbol} {qty_str} {p.expiration} ${p.strike} {p.option_type}"


def format_local_briefing(
    regime: RegimeState,
    vix: float,
    spy_change: float,
    all_signals: list[AlphaSignal],
    watchlist_data: list[tuple[str, MarketContext, PriceHistory, OptionsChain, EventCalendar]],
    tax_alerts: list[str],
    recommendations: list[SizedOpportunity] | None = None,
    intel_contexts: list[IntelligenceContext] | None = None,
    analyst_brief: str | None = None,
    position_reviews: list[PositionReview] | None = None,
    tax_engine: Any | None = None,
    portfolio_state: Any | None = None,
    scanner_picks: list[ScannerPick] | None = None,
    bench: list[BenchEntry] | None = None,
    leap_candidates: list[LeapCandidate] | None = None,
) -> str:
    """Format an action-oriented briefing. Structure: DO NOW → CONSIDER → WATCH → MARKET."""
    from datetime import date, timedelta

    today = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    regime_tag = {"attack": "ATTACK", "hold": "HOLD", "defend": "DEFEND", "crisis": "CRISIS"}
    lines: list[str] = []

    # Defaults — overridden if portfolio_state is available
    nlv = portfolio_state.net_liquidation if portfolio_state else Decimal("0")
    cash = portfolio_state.cash_available if portfolio_state else Decimal("0")

    # Header — compact, regime baked in
    regime_name = regime_tag.get(regime.regime, regime.regime.upper())
    regime_colors = {"ATTACK": _C.green, "HOLD": _C.yellow, "DEFEND": _C.red, "CRISIS": _C.red}
    regime_fn = regime_colors.get(regime_name, _C.yellow)
    spy_fn = _C.green if spy_change >= 0 else _C.red

    regime_emoji = {"ATTACK": "🟢", "HOLD": "🟡", "DEFEND": "🟠", "CRISIS": "🔴"}
    r_emoji = regime_emoji.get(regime_name, "🟡")

    lines.append(f"{'=' * 44}")
    lines.append(_C.bold(f"  🎡 WHEEL COPILOT — {today}"))
    lines.append(f"  {r_emoji} {regime_fn(regime_name)} "
                 f"| VIX {vix:.1f} | SPY {spy_fn(f'{spy_change:+.2%}')}")
    if portfolio_state and portfolio_state.net_liquidation > 0:
        nlv = portfolio_state.net_liquidation
        cash = portfolio_state.cash_available
        deployed_pct = float((nlv - cash) / nlv) if nlv else 0
        lines.append(f"  💰 NLV: {_C.bold(f'${nlv:,.0f}')} "
                     f"| Cash: {_C.green(f'${cash:,.0f}')} "
                     f"| Deployed: {deployed_pct:.0%}")
    if tax_engine:
        net_ytd = (tax_engine.realized_stcg_ytd + tax_engine.realized_ltcg_ytd
                   - tax_engine.realized_losses_ytd)
        net_fn = _C.green if net_ytd >= 0 else _C.red
        lines.append(f"  📊 YTD P&L: {net_fn(f'${net_ytd:+,.0f}')} "
                     f"| Premium: {_C.green(f'${tax_engine.option_premium_income_ytd:,.0f}')}")
    lines.append(f"{'=' * 44}")

    # ── Portfolio exposure map — used by all sections for concentration checks ──
    # Aggregates all positions (stock + options collateral) by symbol as % of NLV
    _symbol_exposure: dict[str, float] = {}  # symbol → total exposure in dollars
    _symbol_option_count: dict[str, int] = {}  # symbol → number of open option positions
    if portfolio_state and portfolio_state.positions and nlv > 0:
        for pos in portfolio_state.positions:
            sym = pos.symbol
            if pos.position_type == "long_stock":
                val = float(pos.underlying_price) * pos.quantity
                _symbol_exposure[sym] = _symbol_exposure.get(sym, 0) + val
            elif pos.option_type:  # any option position
                # Collateral estimate: strike * 100 * quantity for short options
                if "short" in pos.position_type:
                    val = float(pos.strike) * 100 * pos.quantity
                else:
                    val = float(pos.market_value) if pos.market_value else 0
                _symbol_exposure[sym] = _symbol_exposure.get(sym, 0) + val
                _symbol_option_count[sym] = _symbol_option_count.get(sym, 0) + 1
    _nlv_f = float(nlv) if nlv > 0 else 1.0

    def _over_concentration(sym: str, threshold: float = 0.10) -> bool:
        """Check if a symbol already exceeds the given NLV threshold.

        Default 10% matches CLAUDE.md rule: 'NEVER exceed 10% NLV in any single name'.
        """
        return _symbol_exposure.get(sym, 0) / _nlv_f > threshold

    # ── DO NOW — urgent position actions + high conviction trades ──
    # Collect urgent items
    urgent_positions = []
    if position_reviews:
        urgent_positions = [p for p in position_reviews
                           if p.action in ("CLOSE NOW", "TAKE PROFIT")]
    high_trades = []
    if recommendations:
        high_trades = [r for r in recommendations if r.conviction == "high"]

    if urgent_positions or high_trades:
        lines.append(f"\n🚨 {_C.red(_C.bold('DO NOW'))}")

        for p in urgent_positions:
            pos_desc = _format_position_desc(p)
            if p.action == "CLOSE NOW":
                action_label = f"🛑 {_C.red('CLOSE NOW')}"
            else:
                action_label = f"✅ {_C.green('TAKE PROFIT')}"
            lines.append(f"  {action_label}: {_C.bold(pos_desc)}")
            lines.append(f"    P&L: {_pnl_colored(p.current_pnl, p.pnl_pct)} | {p.days_to_expiry}d left")
            lines.append(f"    {p.reasoning}")
            if p.roll:
                r = p.roll
                opt_letter = "C" if p.option_type == "call" else "P"
                credit_str = f"${r.net_credit:,.2f} credit" if r.net_credit >= 0 else f"${abs(r.net_credit):,.2f} debit"
                total_str = f"+${r.total_net:,.0f}" if r.total_net >= 0 else f"-${abs(r.total_net):,.0f}"
                lines.append(f"    ROLL: Buy back ${p.strike} {opt_letter} @ ${r.close_price:.2f} → "
                             f"Sell ${r.new_strike} {opt_letter} exp {r.new_expiration} @ ${r.new_premium:.2f}")
                lines.append(f"          Net {credit_str}/contract ({total_str} total) [{r.roll_type}]")
                # Risk metrics
                if r.risk:
                    rk = r.risk
                    lines.append(f"          Delta {rk.delta:.2f} | IV {rk.iv:.0%} | "
                                 f"Collateral ${rk.collateral:,.0f}")
                    if rk.loss_at_10pct_drop > 0 or rk.loss_at_20pct_drop > 0:
                        s10 = _format_stress_value(rk.loss_at_10pct_drop)
                        s20 = _format_stress_value(rk.loss_at_20pct_drop)
                        lines.append(f"          Stress: 10% drop → {s10} | "
                                     f"20% drop → {s20} | "
                                     f"R/R {rk.risk_reward:.1f}:1")
                    for w in rk.warnings:
                        lines.append(f"          !! {w}")

        for r in high_trades:
            # Skip if symbol already has >= 3 option positions open
            if _symbol_option_count.get(r.symbol, 0) >= 3:
                continue
            # Skip if adding this trade would push symbol over 10% NLV
            # (only apply when NLV is known; skip check when no portfolio data)
            if nlv > 0:
                new_exposure = float(r.strike) * 100 * r.contracts
                if (_symbol_exposure.get(r.symbol, 0) + new_exposure) / _nlv_f > 0.10:
                    continue

            exp_str = _fmt_exp(r.expiration) if r.expiration else "~30 DTE"
            delta_str = f" | delta {r.smart_strike.delta:.2f}" if r.smart_strike else ""
            tv_str = ""
            if intel_contexts:
                ctx_match = next((c for c in intel_contexts if c.symbol == r.symbol), None)
                if ctx_match and ctx_match.technical_consensus:
                    tv_str = f" | TV {ctx_match.technical_consensus.overall}"

            is_call = r.trade_type == "sell_call"
            trade_label = "Sell Covered Call" if is_call else "Sell Cash-Secured Put"
            opt_letter = "C" if is_call else "P"
            lines.append(f"  >>> {_C.bold(r.symbol)} — {_C.green(trade_label)}")
            if r.conviction_label:
                lines.append(f"      {r.conviction_label}")
            lines.append(
                f"      {r.contracts}x ${r.strike} {opt_letter} exp {exp_str} "
                f"@ ${r.premium} mid"
            )
            lines.append(
                f"      {r.annualized_yield:.0%} ann. yield{delta_str}{tv_str}"
            )
            if is_call:
                lines.append(
                    f"      Max profit ${r.premium * r.contracts * 100:,.0f}"
                )
            else:
                lines.append(
                    f"      Collateral ${r.capital_deployed:,.0f} "
                    f"({r.portfolio_pct:.1%} NLV) | "
                    f"Max profit ${r.premium * r.contracts * 100:,.0f}"
                )
            sig_names = ", ".join(s.signal_type.value for s in r.signals)
            if sig_names:
                lines.append(f"      Why: {sig_names}")
            if r.smart_strike and r.smart_strike.technical_reason:
                lines.append(f"      Strike at {r.smart_strike.technical_reason}")

    # ── CONSIDER — medium conviction trades + TV opportunities ──
    medium_trades = []
    if recommendations:
        medium_trades = [r for r in recommendations if r.conviction == "medium"]

    # Low conviction trades (includes TV-driven with no quant signals)
    low_trades = []
    if recommendations:
        low_trades = [r for r in recommendations if r.conviction == "low"]

    consider_lines: list[str] = []
    if medium_trades or low_trades:
        for r in medium_trades + low_trades:
            # Same concentration / position-cap guards as DO NOW
            if _symbol_option_count.get(r.symbol, 0) >= 3:
                continue
            # Only apply concentration check when NLV is known
            if nlv > 0:
                new_exposure = float(r.strike) * 100 * r.contracts
                if (_symbol_exposure.get(r.symbol, 0) + new_exposure) / _nlv_f > 0.10:
                    continue

            exp_str = _fmt_exp(r.expiration) if r.expiration else "~30 DTE"
            delta_str = f" | delta {r.smart_strike.delta:.2f}" if r.smart_strike else ""
            tv_str = ""
            if intel_contexts:
                ctx_match = next((c for c in intel_contexts if c.symbol == r.symbol), None)
                if ctx_match and ctx_match.technical_consensus:
                    tv_str = f" | TV {ctx_match.technical_consensus.overall}"

            is_call = r.trade_type == "sell_call"
            opt_type = "Call" if is_call else "Put"
            consider_lines.append(f"   >> {_C.bold(r.symbol)} — Sell ${r.strike} {opt_type} exp {exp_str}")
            if r.conviction_label:
                consider_lines.append(f"      {r.conviction_label}")
            consider_lines.append(
                f"      {r.contracts}x @ ${r.premium} mid | "
                f"{r.annualized_yield:.0%} ann.{delta_str}{tv_str}"
            )
            if is_call:
                consider_lines.append(
                    f"      Max profit ${r.premium * r.contracts * 100:,.0f}"
                )
            else:
                consider_lines.append(
                    f"      Collateral ${r.capital_deployed:,.0f} "
                    f"({r.portfolio_pct:.1%} NLV) | "
                    f"Max profit ${r.premium * r.contracts * 100:,.0f}"
                )
            consider_lines.append(f"      {r.reasoning}")

    if consider_lines:
        lines.append(f"\n💡 {_C.blue(_C.bold('CONSIDER'))}")
        lines.extend(consider_lines)

    # ── OPPORTUNITIES — proactive deployment recommendations ──
    # Symbols being closed/rolled in DO NOW — don't recommend re-opening
    _closing_symbols: set[str] = set()
    for p in urgent_positions:
        if p.action == "CLOSE NOW":
            _closing_symbols.add(p.symbol)

    # Evaluate watchlist names NOT already covered by signal-driven recommendations
    rec_symbols: set[str] = set()
    if recommendations:
        rec_symbols.update(r.symbol for r in recommendations)

    # (symbol, type, reason, details, option_contract_or_None)
    opportunities: list[tuple[str, str, str, list[str], "OptionContract | None"]] = []

    # TV consensus and options intel by symbol
    tv_by_sym: dict[str, str] = {}
    options_intel_by_sym: dict[str, "OptionsIntelligence"] = {}
    if intel_contexts:
        for ctx in intel_contexts:
            if ctx.technical_consensus:
                tv_by_sym[ctx.symbol] = ctx.technical_consensus.overall
            if ctx.options:
                options_intel_by_sym[ctx.symbol] = ctx.options

    for symbol, mkt, hist, chain, cal in watchlist_data:
        if symbol in rec_symbols:
            continue  # already has a signal-driven recommendation

        # Don't recommend opening what we're closing in DO NOW
        if symbol in _closing_symbols:
            continue

        # Skip names already over-concentrated (>5% NLV)
        if _over_concentration(symbol):
            continue

        # Skip if we already have an open option position on this name
        if _symbol_option_count.get(symbol, 0) > 0:
            continue

        # ADBE: reducing concentration per policy — don't add more
        if symbol == "ADBE":
            continue

        tv = tv_by_sym.get(symbol, "")
        rsi = hist.rsi_14
        price = float(mkt.price) if mkt.price else 0
        iv = mkt.iv_rank

        # Skip names with bearish TV consensus
        if tv in ("SELL", "STRONG_SELL"):
            continue

        # Skip overbought stocks — don't sell puts into a potential reversal
        if rsi is not None and rsi > 65:
            continue

        # Skip if earnings within 7 days (don't open anything near earnings)
        if cal.next_earnings and cal.next_earnings <= date.today() + timedelta(days=7):
            continue
        # Note: further earnings check happens when selecting put candidates —
        # we only recommend puts that expire BEFORE earnings.

        details: list[str] = []
        score = 0  # higher = more attractive

        # Near support levels
        sma200 = float(hist.sma_200) if hist.sma_200 else None
        sma50 = float(hist.sma_50) if hist.sma_50 else None

        if sma200 and price > 0:
            pct_from_200 = (price - sma200) / sma200
            if -0.05 <= pct_from_200 <= 0.03:
                score += 2
                details.append(f"Near 200 SMA (${sma200:,.0f})")

        if sma50 and price > 0:
            pct_from_50 = (price - sma50) / sma50
            if -0.05 <= pct_from_50 <= 0.02:
                score += 1
                details.append(f"Near 50 SMA (${sma50:,.0f})")

        # RSI pullback (not oversold enough for a signal, but attractive)
        if rsi is not None and 30 <= rsi <= 45:
            score += 2
            details.append(f"RSI {rsi:.0f} — pullback territory")
        elif rsi is not None and rsi < 30:
            score += 3
            details.append(f"RSI {rsi:.0f} — oversold")

        # TV consensus positive
        if tv == "STRONG_BUY":
            score += 3
            details.append(f"TV STRONG_BUY")
        elif tv == "BUY":
            score += 2
            details.append(f"TV BUY")

        # 52-week range — near low end is attractive for buys
        if hist.high_52w > 0 and hist.low_52w > 0:
            range_52w = float(hist.high_52w - hist.low_52w)
            if range_52w > 0:
                pct_in_range = (price - float(hist.low_52w)) / range_52w
                if pct_in_range < 0.30:
                    score += 2
                    details.append(f"Bottom 30% of 52w range")

        # IV rank for options selling
        iv_detail = ""
        if iv >= 50:
            score += 2
            iv_detail = f"IV rank {iv:.0f} — rich premium for puts"
        elif iv >= 30:
            score += 1
            iv_detail = f"IV rank {iv:.0f} — decent premium"

        if score < 3:
            continue  # not enough conviction

        # Determine recommendation type — wheel-focused
        # SELL PUT is the default wheel entry: collect premium, get assigned at discount
        # BUY 100 SHARES only if affordable (need 100 for covered calls) AND IV is low
        rec_type = ""
        reason = ""
        cost_100 = price * 100
        can_afford_100 = cost_100 > 0 and float(cash) >= cost_100 and cost_100 <= float(nlv) * 0.05

        if iv >= 40 and (rsi is not None and rsi <= 45):
            rec_type = "SELL PUT"
            reason = f"Sell put on pullback — {'rich' if iv >= 50 else 'decent'} premium"
            if iv_detail:
                details.append(iv_detail)
        elif iv >= 50:
            rec_type = "SELL PUT"
            reason = f"Premium rich (IV rank {iv:.0f}) — sell puts to enter at a discount"
        elif can_afford_100 and score >= 4 and tv in ("BUY", "STRONG_BUY") and iv < 30:
            # Low IV = cheap puts, better to buy shares and start selling calls
            rec_type = "BUY 100 SHARES"
            reason = "Start wheel — IV too low for puts, buy shares and sell covered calls"
        else:
            # Default wheel entry: sell puts to collect premium or get assigned
            rec_type = "SELL PUT"
            reason = "Sell puts to enter at a discount — wheel entry"
            if iv_detail:
                details.append(iv_detail)

        # For SELL PUT, find the best put contract from the chain
        best_put = None
        if rec_type == "SELL PUT" and chain and chain.puts:
            # Target: 0.20-0.30 delta, 30-45 DTE, expires BEFORE earnings
            today = date.today()
            candidates = [
                p for p in chain.puts
                if 0.10 <= abs(p.delta) <= 0.35
                and 20 <= (p.expiration - today).days <= 55
                and p.bid > 0
                and not (cal.next_earnings and p.expiration >= cal.next_earnings)
            ]
            if candidates:
                # Prefer closest to 0.25 delta in the 30-45 DTE range
                best_put = min(
                    candidates,
                    key=lambda p: (abs(abs(p.delta) - 0.25) + abs((p.expiration - today).days - 37) / 100)
                )

        # Skip SELL PUT if we couldn't find a valid contract (e.g., all expire after earnings)
        if rec_type == "SELL PUT" and not best_put:
            continue
        if rec_type:
            opportunities.append((symbol, rec_type, reason, details, best_put))

    # Sort by detail count (proxy for conviction) descending
    opportunities.sort(key=lambda x: len(x[3]), reverse=True)

    # ── Reallocation candidates: underperforming stock that could be redeployed ──
    _INDEX_ETFS = {"VOO", "SPY", "IWM", "QQQ", "SMH", "DIA", "VTI", "SCHD"}
    realloc_candidates: list[tuple[str, int, Decimal, Decimal, float, str]] = []
    # (symbol, shares, cost_basis_per_share, current_price, pnl_pct, reason)

    # Aggregate stock positions by symbol first (handles split lots across accounts)
    _agg_stocks: dict[str, dict] = {}
    if portfolio_state and portfolio_state.positions:
        for pos in portfolio_state.positions:
            if pos.position_type != "long_stock" or pos.quantity < 1:
                continue
            sym = pos.symbol
            if sym not in _agg_stocks:
                _agg_stocks[sym] = {"quantity": 0, "total_cost": Decimal("0"),
                                    "price": pos.underlying_price}
            _agg_stocks[sym]["quantity"] += pos.quantity
            # Normalize per-share cost basis
            cb = pos.cost_basis
            if pos.quantity > 1 and cb > pos.underlying_price * 3:
                cb = cb / pos.quantity
            _agg_stocks[sym]["total_cost"] += cb * pos.quantity
            # Use latest price seen
            if pos.underlying_price > 0:
                _agg_stocks[sym]["price"] = pos.underlying_price

    for sym, agg in _agg_stocks.items():
        if sym in _INDEX_ETFS:
            continue
        qty = agg["quantity"]
        cur_price = agg["price"]
        if cur_price <= 0 or qty < 1:
            continue
        total_cost = agg["total_cost"]
        if total_cost <= 0:
            continue
        per_share_cost = total_cost / qty
        pnl_pct = float((cur_price - per_share_cost) / per_share_cost)

        # Skip profitable winners — don't sell what's working
        if pnl_pct > 0.15:
            continue

        sym_tv = tv_by_sym.get(sym, "")

        # Find underperformers: down significantly OR bearish consensus
        reason_parts: list[str] = []
        if pnl_pct < -0.15:
            reason_parts.append(f"down {pnl_pct:.0%} from cost basis")
        elif pnl_pct < -0.05 and sym_tv in ("SELL", "STRONG_SELL"):
            reason_parts.append(f"down {pnl_pct:.0%}, TV {sym_tv}")
        # Small position that can't run the wheel (< 100 shares, not near 100)
        if qty < 100 and qty < 90:
            reason_parts.append(f"only {qty} shares — can't sell covered calls")

        if reason_parts:
            realloc_candidates.append((
                sym, qty, per_share_cost,
                cur_price, pnl_pct, " | ".join(reason_parts),
            ))

    # Sort by worst performer first
    realloc_candidates.sort(key=lambda x: x[4])

    if opportunities and cash > 0:
        lines.append(f"\n🎯 {_C.green(_C.bold('OPPORTUNITIES'))} "
                     f"— ${cash:,.0f} cash available")

        for symbol, rec_type, reason, details, put_contract in opportunities[:8]:
            _, mkt, hist, _, _ = next(
                (w for w in watchlist_data if w[0] == symbol), (None,)*5
            )
            price = float(mkt.price) if mkt else 0

            # Skip low-yield puts — not worth the collateral
            ann_yield = 0.0
            if rec_type == "SELL PUT" and put_contract:
                dte = (put_contract.expiration - date.today()).days
                strike_f = float(put_contract.strike)
                mid = float(put_contract.mid)
                if strike_f > 0 and dte > 0:
                    yield_on_cap = (mid / strike_f) * 100
                    ann_yield = yield_on_cap * (365 / dte)
                if ann_yield < 25:
                    continue  # not enough premium to justify tying up capital

            if rec_type == "BUY 100 SHARES":
                type_label = f"🟩 {_C.green('BUY 100 SHARES')}"
            else:
                type_label = f"📝 {_C.cyan('SELL PUT')}"
            lines.append(f"  {type_label}: {_C.bold(symbol)} @ ${price:,.2f}")
            lines.append(f"    {reason}")
            if details:
                lines.append(f"    {' | '.join(details)}")

            # Option contract details for SELL PUT
            if rec_type == "SELL PUT" and put_contract:
                dte = (put_contract.expiration - date.today()).days
                mid = float(put_contract.mid)
                bid = float(put_contract.bid)
                strike_f = float(put_contract.strike)
                yield_on_cap = (mid / strike_f) * 100 if strike_f > 0 else 0
                lines.append(
                    f"    {_C.bold('Strike')}: ${put_contract.strike} "
                    f"| {_C.bold('Exp')}: {_fmt_exp(put_contract.expiration)} ({dte}d) "
                    f"| {_C.bold('Bid')}: ${bid:.2f} "
                    f"| {_C.bold('Delta')}: {abs(put_contract.delta):.2f}"
                )
                lines.append(
                    f"    Premium: ${mid:.2f}/contract "
                    f"| Yield: {yield_on_cap:.1f}% ({ann_yield:.0f}% ann)"
                )

            # Sizing: 1.5% NLV target, hard cap at 5% NLV, max 10 contracts
            if nlv > 0:
                target_alloc = float(nlv) * 0.015
                max_alloc = float(nlv) * 0.05
                if rec_type == "BUY 100 SHARES":
                    cost = price * 100
                    if cost > max_alloc:
                        continue  # too expensive, skip
                    lines.append(f"    Size: 100 shares (${cost:,.0f}, "
                                 f"~{cost / float(nlv):.1%} NLV) — then sell covered calls")
                elif put_contract:
                    strike_f = float(put_contract.strike)
                    contracts = max(1, int(target_alloc / (strike_f * 100))) if strike_f > 0 else 1
                    contracts = min(contracts, 10)  # hard cap — no 56x WTI situations
                    collateral = Decimal(contracts) * put_contract.strike * 100
                    # Cap at 5% NLV
                    while float(collateral) > max_alloc and contracts > 1:
                        contracts -= 1
                        collateral = Decimal(contracts) * put_contract.strike * 100
                    total_premium = Decimal(contracts) * put_contract.mid * 100
                    lines.append(
                        f"    Size: {contracts}x ${put_contract.strike} puts "
                        f"(${collateral:,.0f} collateral, ${total_premium:,.0f} premium, "
                        f"~{float(collateral) / float(nlv):.1%} NLV)"
                    )
                else:
                    # SELL PUT without chain data — fallback to price-based estimate
                    contracts = max(1, int(target_alloc / (price * 100))) if price > 0 else 1
                    contracts = min(contracts, 10)
                    collateral = contracts * price * 100
                    lines.append(f"    Size: ~{contracts}x puts (${collateral:,.0f} collateral, "
                                 f"~{collateral / float(nlv):.1%} NLV)")

    # ── SCANNER PICKS — high-IV wheel candidates from broader universe ──
    scanner_lines: list[str] = []
    if scanner_picks:
        max_alloc_scanner = float(nlv) * 0.02 if nlv > 0 else 20_000
        shown = 0
        for pick in scanner_picks:
            if shown >= 6:
                break
            # Price floor — scanner cache may contain stale symbols that dropped
            if pick.price < 5:
                continue
            # Skip if even 1 contract exceeds 2% NLV
            if pick.collateral_per_contract > max_alloc_scanner:
                continue
            # Skip if already concentrated in this name
            if _over_concentration(pick.symbol):
                continue
            # Skip if we already have an open option position on this name
            if _symbol_option_count.get(pick.symbol, 0) > 0:
                continue
            # Don't recommend what we're closing in DO NOW
            if pick.symbol in _closing_symbols:
                continue
            # Minimum premium per contract — $1.00 ($100/contract) floor.
            # Tiny premiums like $0.30 aren't worth the collateral or attention.
            if pick.put_contract and float(pick.put_contract.bid) < 1.00:
                continue
            # Bid-ask spread check — skip illiquid options
            if pick.put_contract:
                pc_bid = float(pick.put_contract.bid)
                pc_ask = float(pick.put_contract.ask)
                pc_mid = float(pick.put_contract.mid)
                if pc_bid > 0 and pc_ask > 0 and pc_mid > 0:
                    spread_pct = (pc_ask - pc_bid) / pc_mid
                    if spread_pct > 0.50:
                        continue  # too wide — illiquid options

            # Tier label from market cap — skip small caps (T3)
            if pick.market_cap >= 10_000_000_000:
                tier = "T1"
            elif pick.market_cap >= 2_000_000_000:
                tier = "T2"
            elif pick.market_cap > 0:
                tier = "T3"
            else:
                tier = ""
            # T3 (small cap < $2B) too risky for scanner picks
            if tier == "T3":
                continue
            tier_str = f" [{tier}]" if tier else ""

            shown += 1
            scanner_lines.append(f"  📝 {_C.cyan('SELL PUT')}: {_C.bold(pick.symbol)}{tier_str} @ ${pick.price:,.2f}")
            scanner_lines.append(f"    {' | '.join(pick.reasons)}")
            if pick.put_contract:
                pc = pick.put_contract
                dte = (pc.expiration - date.today()).days
                delta_str = f" | {_C.bold('Delta')}: {abs(pc.delta):.2f}" if abs(pc.delta) > 0.01 else ""
                otm_pct = (1 - float(pc.strike) / pick.price) * 100 if pick.price > 0 else 0
                scanner_lines.append(
                    f"    {_C.bold('Strike')}: ${pc.strike} ({otm_pct:.0f}% OTM) "
                    f"| {_C.bold('Exp')}: {_fmt_exp(pc.expiration)} ({dte}d) "
                    f"| {_C.bold('Bid')}: ${float(pc.bid):.2f}"
                    f"{delta_str}"
                )
                mid = float(pc.mid)
                strike_f = float(pc.strike)
                yield_pct = (mid / strike_f) * 100 if strike_f > 0 else 0
                scanner_lines.append(
                    f"    Premium: ${mid:.2f}/contract "
                    f"| Yield: {yield_pct:.1f}% ({pick.ann_yield:.0f}% ann)"
                )
            if nlv > 0:
                target_alloc = float(nlv) * 0.01   # conservative for scanner picks
                max_alloc = float(nlv) * 0.02
                if pick.put_contract:
                    strike_f = float(pick.put_contract.strike)
                    contracts = max(1, int(target_alloc / (strike_f * 100))) if strike_f > 0 else 1
                    contracts = min(contracts, 10)
                    collateral = Decimal(contracts) * pick.put_contract.strike * 100
                    while float(collateral) > max_alloc and contracts > 1:
                        contracts -= 1
                        collateral = Decimal(contracts) * pick.put_contract.strike * 100
                    total_premium = Decimal(contracts) * pick.put_contract.mid * 100
                    scanner_lines.append(
                        f"    Size: {contracts}x ${pick.put_contract.strike} puts "
                        f"(${collateral:,.0f} collateral, ${total_premium:,.0f} premium, "
                        f"~{float(collateral) / float(nlv):.1%} NLV)"
                    )
                else:
                    contracts = max(1, int(target_alloc / (pick.price * 100))) if pick.price > 0 else 1
                    contracts = min(contracts, 10)
                    coll = contracts * pick.price * 100
                    scanner_lines.append(f"    Size: ~{contracts}x puts (${coll:,.0f} collateral, "
                                         f"~{coll / float(nlv):.1%} NLV)")

    if scanner_lines:
        if not opportunities:
            lines.append(f"\n🎯 {_C.green(_C.bold('OPPORTUNITIES'))} "
                         f"— ${cash:,.0f} cash available")
        # Determine header based on whether picks came from shopping list
        has_sl_picks = scanner_picks and any(p.shopping_list_rating for p in scanner_picks)
        has_finviz = scanner_picks and any(p.shopping_list_rating is None for p in scanner_picks)
        if has_sl_picks and has_finviz:
            scanner_header = "from your shopping list + scanner"
        elif has_sl_picks:
            scanner_header = "from your shopping list"
        else:
            scanner_header = "high-IV names outside your watchlist"
        lines.append(f"\n  🔍 {_C.bold('SCANNER PICKS')} — {scanner_header}")
        lines.extend(scanner_lines)

    # ── BENCH — shopping list names approaching entry ──
    if bench:
        lines.append(f"\n📋 {_C.bold('BENCH')} — shopping list names approaching entry")
        for b in bench:
            # Price target + upside
            target_str = ""
            if b.price_target:
                if b.upside_pct is not None:
                    target_str = f" → ${b.price_target} ({b.upside_pct:+.0%})"
                else:
                    target_str = f" → ${b.price_target}"

            # Earnings display
            earns_str = ""
            if b.next_earnings:
                earns_str = f" | Earns {b.next_earnings.strftime('%b')} {b.next_earnings.day}"

            if b.near_actionable:
                # Expanded format — pad ticker BEFORE bold() so ANSI codes don't break width
                lines.append(
                    f"  🔥 {_C.bold(b.ticker.ljust(8))} {b.rating:12s} "
                    f"${b.current_price:<7,.0f}{target_str} "
                    f"| IV {b.iv_rank:.0f} | RSI {b.rsi:.0f}{earns_str}"
                )
                lines.append(f"     READY: {b.actionable_reason}")
            else:
                # Compact one-liner
                lines.append(
                    f"  {b.ticker:8s} {b.rating:12s} "
                    f"${b.current_price:<7,.0f}{target_str} "
                    f"| IV {b.iv_rank:.0f} | RSI {b.rsi:.0f}{earns_str}"
                )

    # ── LEAP RADAR — low-IV names where buying calls beats selling premium ──
    if leap_candidates:
        # Only show candidates that have real chain data
        with_chain = [lc for lc in leap_candidates if lc.strike is not None]
        without_chain = [lc for lc in leap_candidates if lc.strike is None]

        if with_chain or without_chain:
            lines.append(f"\n🚀 {_C.magenta(_C.bold('LEAP RADAR'))} "
                         f"— IV is low, consider buying calls instead of selling")

        for lc in with_chain:
            lines.append(f"  📈 {_C.green('BUY LEAP CALL')}: {_C.bold(lc.symbol)} @ ${lc.price:,.2f}")
            lines.append(f"    {' | '.join(lc.reasons)}")
            # Real chain data
            delta_str = f" | {_C.bold('Delta')}: {lc.delta:.2f}" if lc.delta else ""
            # For calls: strike < price = ITM
            below_pct = (lc.price - float(lc.strike)) / lc.price * 100 if lc.price > 0 else 0
            itm_label = f"{below_pct:.0f}% ITM" if below_pct > 0 else f"{abs(below_pct):.0f}% OTM"
            lines.append(
                f"    {_C.bold('Strike')}: ${lc.strike} ({itm_label}) "
                f"| {_C.bold('Exp')}: {lc.expiration} ({lc.dte}d) "
                f"| {_C.bold('Bid')}: ${lc.bid:.2f} "
                f"| {_C.bold('Ask')}: ${lc.ask:.2f}"
                f"{delta_str}"
            )
            if lc.open_interest and lc.open_interest > 0:
                oi_str = f" | OI: {lc.open_interest:,}"
            else:
                oi_str = ""
            spread = lc.ask - lc.bid if lc.ask and lc.bid else 0
            spread_pct = (spread / lc.mid * 100) if lc.mid and lc.mid > 0 else 0
            spread_warn = f" {_C.yellow('(wide)')}" if spread_pct > 10 else ""
            lines.append(
                f"    Mid: ${lc.mid:.2f}/contract (${lc.mid * 100:,.0f} total) "
                f"| Spread: ${spread:.2f} ({spread_pct:.0f}%){spread_warn}{oi_str}"
            )
            # Size: 2-3% of NLV max per LEAP position
            if nlv > 0 and lc.mid:
                max_leap = float(nlv) * 0.03
                cost_per = lc.mid * 100
                contracts = max(1, int(max_leap / cost_per)) if cost_per > 0 else 1
                contracts = min(contracts, 5)
                total_cost = contracts * cost_per
                lines.append(
                    f"    Size: {contracts}x ${lc.strike} calls "
                    f"(${total_cost:,.0f}, ~{total_cost / float(nlv):.1%} NLV)"
                )

        for lc in without_chain:
            lines.append(f"  📈 {_C.green('BUY LEAP CALL')}: {_C.bold(lc.symbol)} @ ${lc.price:,.2f}")
            lines.append(f"    {' | '.join(lc.reasons)}")
            if lc.expiration:
                lines.append(f"    Target: ~0.70 delta call, {lc.expiration} ({lc.dte}d) "
                             f"| chain unavailable — check manually")
            else:
                lines.append(f"    Target: ~0.70 delta call, 12+ months out "
                             f"| no LEAP expirations found")

    # ── REALLOCATE — underperforming positions to redeploy ──
    if realloc_candidates:
        lines.append(f"\n🔄 {_C.yellow(_C.bold('REALLOCATE'))} "
                     f"— sell underperformers, redeploy into wheel")
        for sym, qty, basis, cur_price, pnl_pct, reason in realloc_candidates[:5]:
            value = qty * cur_price
            pnl_dollar = qty * (cur_price - basis)
            pnl_color = _C.red if pnl_pct < 0 else _C.green
            lines.append(
                f"  ♻️ {_C.bold(sym)} — {qty} shares @ ${cur_price:,.2f} "
                f"({pnl_color(f'{pnl_pct:+.0%}')}, {pnl_color(f'${pnl_dollar:+,.0f}')})"
            )
            lines.append(f"    {reason}")
            lines.append(f"    💵 Frees ${value:,.0f} → redeploy via puts")

    # ── WATCH — position holds + earnings + tax ──
    watch_positions = []
    if position_reviews:
        watch_positions = [p for p in position_reviews
                          if p.action in ("WATCH CLOSELY", "HOLD")]
    upcoming_earnings = []
    for symbol, _, _, _, cal in watchlist_data:
        if cal.next_earnings and cal.next_earnings <= date.today() + timedelta(days=14):
            days = (cal.next_earnings - date.today()).days
            upcoming_earnings.append((symbol, cal.next_earnings, days))

    has_watch = watch_positions or upcoming_earnings or tax_alerts
    if has_watch:
        lines.append(f"\n👀 {_C.yellow(_C.bold('WATCH'))}")

        for p in watch_positions:
            pos_desc = _format_position_desc(p)
            watch_emoji = "⚠️" if p.action == "WATCH CLOSELY" else "📌"
            lines.append(f"  {watch_emoji} {_C.bold(pos_desc)}")
            if "\n" in p.reasoning:
                # Multi-line reasoning: P&L on its own line, reasons indented below
                lines.append(f"    P&L: {_pnl_colored(p.current_pnl, p.pnl_pct)} | {p.days_to_expiry}d left")
                for reason_line in p.reasoning.split("\n"):
                    lines.append(f"    {reason_line}")
            else:
                lines.append(f"    P&L: {_pnl_colored(p.current_pnl, p.pnl_pct)} | {p.days_to_expiry}d left | {p.reasoning}")
            if p.roll:
                r = p.roll
                opt_letter = "C" if p.option_type == "call" else "P"
                credit_fn = _C.green if r.net_credit >= 0 else _C.red
                credit_str = f"${r.net_credit:,.2f} credit" if r.net_credit >= 0 else f"${abs(r.net_credit):,.2f} debit"
                total_str = f"+${r.total_net:,.0f}" if r.total_net >= 0 else f"-${abs(r.total_net):,.0f}"
                lines.append(f"    {_C.cyan('ROLL')}: Buy back ${p.strike} {opt_letter} @ ${r.close_price:.2f} → "
                             f"Sell ${r.new_strike} {opt_letter} exp {r.new_expiration} @ ${r.new_premium:.2f}")
                lines.append(f"          Net {credit_fn(credit_str)}/contract ({credit_fn(total_str)} total) [{r.roll_type}]")
                if r.risk:
                    rk = r.risk
                    if p.option_type == "put":
                        lines.append(f"          Delta {rk.delta:.2f} | IV {rk.iv:.0%} | "
                                     f"Collateral ${rk.collateral:,.0f}")
                    else:
                        lines.append(f"          Delta {rk.delta:.2f} | IV {rk.iv:.0%}")
                    if rk.loss_at_10pct_drop > 0 or rk.loss_at_20pct_drop > 0:
                        s10 = _format_stress_value(rk.loss_at_10pct_drop)
                        s20 = _format_stress_value(rk.loss_at_20pct_drop)
                        lines.append(f"          Stress: 10% drop → {s10} | "
                                     f"20% drop → {s20} | "
                                     f"R/R {rk.risk_reward:.1f}:1")
                    for w in rk.warnings:
                        lines.append(f"          {_C.yellow('!!')} {w}")

        if upcoming_earnings:
            lines.append("")
            for sym, dt, days in sorted(upcoming_earnings, key=lambda x: x[2]):
                label = "TOMORROW" if days <= 1 else f"{days}d"
                lines.append(f"  📅 Earnings: {sym} {dt} ({label})")

        for alert in tax_alerts:
            lines.append(f"  Tax: {alert}")

    # Concentration warnings (cross-position)
    if position_reviews:
        from collections import Counter
        sym_counts = Counter(p.symbol for p in position_reviews)
        concentrated = [(sym, cnt) for sym, cnt in sym_counts.items() if cnt >= 2]
        if concentrated:
            if not has_watch:
                lines.append(f"\n👀 {_C.yellow(_C.bold('WATCH'))}")
            lines.append("")
            for sym, cnt in sorted(concentrated, key=lambda x: -x[1]):
                sym_positions = [p for p in position_reviews if p.symbol == sym]
                total_exposure = sum(abs(p.current_pnl) + abs(p.entry_price * 100 * p.quantity)
                                     for p in sym_positions)
                lines.append(f"  ⚠️ {_C.bold(sym)}: {cnt} positions — "
                             f"watch concentration (max 10% NLV)")

    # ── CAPITAL NOTE — when significant idle cash + take-profits need redeployment ──
    if portfolio_state and nlv > 0:
        cash = float(portfolio_state.cash_available)
        # Estimate collateral freed by take-profit closes
        freed_collateral = Decimal("0")
        for p in urgent_positions:
            if p.action == "TAKE PROFIT" and p.option_type == "put":
                freed_collateral += p.strike * 100 * abs(p.quantity)
        total_idle = cash + float(freed_collateral)
        idle_pct = total_idle / float(nlv)
        # Only show when ≥10% of NLV is idle — worth noting
        if idle_pct >= 0.10:
            # Count earnings-blocked watchlist names
            earnings_blocked = 0
            for symbol, _, _, _, cal in watchlist_data:
                if cal.next_earnings and cal.next_earnings <= date.today() + timedelta(days=30):
                    earnings_blocked += 1
            capital_lines = [
                f"\n💰 {_C.bold('CAPITAL')}",
                f"  Cash: ${cash:,.0f} | Freed by take-profits: ${float(freed_collateral):,.0f} | "
                f"Total deployable: ${total_idle:,.0f} ({idle_pct:.0%} NLV)",
            ]
            if earnings_blocked > 0:
                capital_lines.append(
                    f"  ⏳ {earnings_blocked} watchlist names reporting within 30d — "
                    f"limited deployment window. Watch for post-earnings entry points.")
            lines.extend(capital_lines)

    # ── Nothing to do ──
    if not (urgent_positions or high_trades or medium_trades or low_trades
            or watch_positions):
        lines.append(f"\n  ✅ No signals fired. Sit tight.")

    # ── ANALYST BRIEF — Claude reasoning (when available) ──
    if analyst_brief:
        lines.append(f"\n🧠 {_C.cyan(_C.bold('ANALYST BRIEF'))}")
        lines.append(analyst_brief)

    # ── YTD P&L — realized option performance from E*Trade ──
    if tax_engine and (tax_engine.option_premium_income_ytd > 0
                       or tax_engine.realized_stcg_ytd > 0
                       or tax_engine.realized_losses_ytd > 0):
        lines.append(f"\n💹 {_C.blue(_C.bold('YTD OPTIONS P&L'))}")
        lines.append(f"  Premium collected:  {_C.green(f'${tax_engine.option_premium_income_ytd:>10,.0f}')}")
        lines.append(f"  Realized gains:     {_C.green(f'${tax_engine.realized_stcg_ytd:>10,.0f}')}  (STCG)")
        if tax_engine.realized_ltcg_ytd > 0:
            lines.append(f"  Realized gains:     {_C.green(f'${tax_engine.realized_ltcg_ytd:>10,.0f}')}  (LTCG)")
        if tax_engine.realized_losses_ytd > 0:
            lines.append(f"  Realized losses:    {_C.red(f'${tax_engine.realized_losses_ytd:>10,.0f}')}")
        net = (tax_engine.realized_stcg_ytd + tax_engine.realized_ltcg_ytd
               - tax_engine.realized_losses_ytd)
        net_fn = _C.green if net >= 0 else _C.red
        lines.append(f"  {'─' * 35}")
        lines.append(f"  Net realized:       {net_fn(f'${net:>+10,.0f}')}")
        # Estimated tax
        est_tax = (
            max(Decimal("0"), tax_engine.realized_stcg_ytd - tax_engine.harvested_losses_ytd)
            * Decimal(str(tax_engine.stcg_effective))
            + tax_engine.realized_ltcg_ytd * Decimal(str(tax_engine.ltcg_effective))
        )
        if est_tax > 0:
            lines.append(f"  Est. tax liability:  {_C.yellow(f'${est_tax:>9,.0f}')}  "
                         f"(Q next: ${est_tax / 4:,.0f})")
        blocked = tax_engine.wash_sale_tracker.get_blocked_tickers()
        if blocked:
            lines.append(f"  Wash sale blocks:   {_C.red(', '.join(blocked))}")

    # ── SKIP — names the system looked at and explicitly rejected ──
    # Collect symbols already covered in DO NOW / CONSIDER / WATCH / OPPORTUNITIES
    covered: set[str] = set()
    if recommendations:
        covered.update(r.symbol for r in recommendations)
    if position_reviews:
        covered.update(p.symbol for p in position_reviews)
    # Opportunity names are already recommended — don't show in SKIP
    covered.update(sym for sym, _, _, _, _ in opportunities)

    # Build skip reasons for uncovered symbols
    tv_by_symbol: dict[str, str] = {}
    if intel_contexts:
        for ctx in intel_contexts:
            if ctx.technical_consensus:
                tv_by_symbol[ctx.symbol] = ctx.technical_consensus.overall

    skips: list[str] = []
    for symbol, mkt, hist, _, _ in watchlist_data:
        if symbol in covered:
            continue
        iv = mkt.iv_rank
        rsi = hist.rsi_14
        tv = tv_by_symbol.get(symbol, "")

        # Determine skip reason (most important reason first)
        if tv in ("SELL", "STRONG_SELL"):
            if iv >= 60:
                skips.append(f"  {symbol}: TV {tv} — rich premium (IV {iv:.0f}) "
                             f"but crowd is bearish. Wait for turn.")
            else:
                skips.append(f"  {symbol}: TV {tv} — bearish consensus. Stay away.")
        elif iv > 0 and iv < 20:
            skips.append(f"  {symbol}: IV rank {iv:.0f} — no premium to sell.")
        elif rsi is not None and rsi > 75:
            skips.append(f"  {symbol}: RSI {rsi:.0f} — overbought, "
                         f"not the time to sell puts.")
        else:
            skips.append(f"  {symbol}: No signal convergence. Sit tight.")

    if skips:
        lines.append(f"\n⏭️ {_C.dim('SKIP')}")
        for s in skips:
            lines.append(_C.dim(s))

    lines.append(_C.dim(f"\n{'=' * 60}"))
    return "\n".join(lines)


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from text."""
    import re
    return re.sub(r'\033\[[0-9;]*m', '', text)


def format_html_briefing(ansi_briefing: str) -> str:
    """Convert the ANSI-colored briefing to HTML for Telegram/email.

    Maps ANSI color codes to HTML spans. Preserves structure with <pre>
    for monospace layout. Telegram supports a subset of HTML —
    this uses only <b>, <span style>, and <pre>.
    """
    import re

    # Strip ANSI and rebuild with HTML
    html = ansi_briefing

    # Map ANSI codes to HTML spans
    replacements = [
        ("\033[91m", '<span style="color:#e74c3c">'),   # red
        ("\033[92m", '<span style="color:#2ecc71">'),   # green
        ("\033[93m", '<span style="color:#f39c12">'),   # yellow
        ("\033[94m", '<span style="color:#3498db">'),   # blue
        ("\033[96m", '<span style="color:#1abc9c">'),   # cyan
        ("\033[2m", '<span style="color:#7f8c8d">'),    # dim
        ("\033[1m", "<b>"),                               # bold
        ("\033[0m", "</span>"),                           # reset → close span
    ]
    for ansi, tag in replacements:
        html = html.replace(ansi, tag)

    # Fix bold resets (bold uses <b> but reset closes </span>)
    # Count open <b> tags and close them properly
    html = re.sub(r'<b>(.*?)</span>', r'<b>\1</b>', html)

    # Wrap in pre for monospace
    html = f'<pre style="font-family:monospace;font-size:13px;line-height:1.4">{html}</pre>'

    return html


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------

async def run_analysis_cycle(
    cycle_name: str,
    always_push: bool = False,
) -> dict[str, Any]:
    """Run a single analysis cycle.

    Pipeline: data -> signals -> regime -> risk -> briefing.
    Uses E*Trade for options chains (real bid/ask) when session is available.
    """
    log.info("analysis_cycle_start", cycle=cycle_name)

    # 0. Try to get E*Trade session for live chain data
    etrade_session = None
    try:
        from src.data.auth import get_session
        etrade_session = get_session()
        log.info("etrade_session_available")
    except Exception:
        log.info("etrade_session_unavailable_using_yfinance")

    # 1. Macro data
    vix, spy_change = fetch_vix_and_spy()
    log.info("macro_data", vix=round(vix, 2), spy_change=round(spy_change, 4))

    # 2. Regime classification
    regime = classify_regime(vix, spy_change)
    log.info("regime_classified", regime=regime.regime, reason=regime.severity)

    # 3. Fetch per-symbol data
    symbols = load_watchlist()
    log.info("fetching_watchlist", symbols=len(symbols))
    watchlist_data = fetch_all_watchlist_data(symbols, etrade_session=etrade_session)
    log.info("watchlist_fetched", symbols_ok=len(watchlist_data))

    # 4. Signal detection on every symbol
    all_signals: list[AlphaSignal] = []
    for symbol, mkt, hist, chain, cal in watchlist_data:
        signals = detect_all_signals(symbol, mkt, hist, chain, cal)
        if signals:
            for s in signals:
                log.info("signal_fired", symbol=s.symbol,
                         signal=s.signal_type.value, strength=s.strength)
        all_signals.extend(signals)

    # 5. Build intelligence contexts
    intel_contexts: list[IntelligenceContext] = []
    by_symbol_signals: dict[str, list[AlphaSignal]] = {}
    for s in all_signals:
        by_symbol_signals.setdefault(s.symbol, []).append(s)

    for symbol, mkt, hist, chain, cal in watchlist_data:
        # TradingView consensus (cached, graceful failure)
        tv_consensus = fetch_tradingview_consensus(symbol)

        ctx = build_intelligence_context(
            symbol=symbol,
            signals=by_symbol_signals.get(symbol, []),
            market=mkt,
            price_history=hist,
            chain=chain,
            calendar=cal,
            technical_consensus=tv_consensus,
        )
        intel_contexts.append(ctx)

    # 5b. Load portfolio and run position review
    chain_by_symbol = {sym: chain for sym, _, _, chain, _ in watchlist_data}
    portfolio_state = None
    position_reviews = []
    try:
        portfolio_state = load_portfolio_state()
        if portfolio_state.positions:
            # Build stock holdings map for covered call detection.
            # A short call is "covered" when you own ≥100 shares per contract.
            stock_shares: dict[str, int] = {}
            for pos in portfolio_state.positions:
                if pos.position_type == "long_stock":
                    stock_shares[pos.symbol] = stock_shares.get(pos.symbol, 0) + pos.quantity

            for pos in portfolio_state.positions:
                # Only review option positions — stocks need different logic
                if not pos.option_type:
                    continue
                # Detect covered calls: short call on a stock we own enough of
                covered = (
                    pos.position_type == "short_call"
                    and stock_shares.get(pos.symbol, 0) >= abs(pos.quantity) * 100
                )
                # Find matching intelligence context
                matching_ctx = next(
                    (c for c in intel_contexts if c.symbol == pos.symbol), None
                )
                if matching_ctx:
                    chain = chain_by_symbol.get(pos.symbol)
                    review = review_position(pos, matching_ctx, chain=chain, is_covered=covered)
                    position_reviews.append(review)
    except Exception as e:
        log.warning("portfolio_load_skipped", error=str(e))

    # 5c. Load shopping list (cached daily)
    shopping_list: list[ShoppingListEntry] = []
    shopping_list_by_ticker: dict[str, ShoppingListEntry] = {}
    try:
        shopping_list = await fetch_shopping_list()
        shopping_list_by_ticker = {e.ticker: e for e in shopping_list}
        log.info("shopping_list_loaded", entries=len(shopping_list))
    except Exception as e:
        log.warning("shopping_list_load_failed", error=str(e))

    # 6. Claude analyst brief (opt-in, requires ANTHROPIC_API_KEY)
    analyst_brief = None
    contexts_with_signals = [c for c in intel_contexts if c.quant.signal_count > 0]
    if contexts_with_signals:
        regime_str = f"{regime.regime.upper()} — VIX {vix:.1f}, SPY {spy_change:+.2%}"
        analyst_brief = await generate_analyst_brief(contexts_with_signals, regime_str)

    # 7. Build sized recommendations from signals (TV adjusts conviction)
    # Pass portfolio_state so covered call eligibility (owned shares) is checked
    recommendations = build_recommendations(
        all_signals, watchlist_data,
        portfolio=portfolio_state,
        intel_contexts=intel_contexts,
        shopping_list=shopping_list_by_ticker,
    )

    # 8. Risk checks + YTD P&L from E*Trade transactions
    router = AccountRouter()
    liquidity_ok, liquidity_msg = check_liquidity_health(router)
    tax_alerts = generate_tax_alerts([])

    tax_engine = None
    if etrade_session:
        try:
            from src.data.broker import fetch_ytd_option_orders, populate_tax_engine_from_orders
            ytd_orders = fetch_ytd_option_orders(etrade_session)
            if ytd_orders:
                tax_engine = populate_tax_engine_from_orders(ytd_orders)
        except Exception as e:
            log.warning("ytd_pnl_fetch_failed", error=str(e))

    # 8b. Scanner — screen broader universe for wheel candidates
    scanner_picks: list[ScannerPick] = []
    try:
        watchlist_set = set(symbols)
        scanner_picks = scan_wheel_candidates(
            watchlist_set, etrade_session=etrade_session,
            shopping_list=shopping_list,
        )
    except Exception as e:
        log.warning("scanner_failed", error=str(e))

    # 8c. Build bench — shopping list names approaching entry
    bench: list[BenchEntry] = []
    try:
        watchlist_set_bench = set(symbols)
        scanner_symbols = {p.symbol for p in scanner_picks}
        bench = await build_bench(
            shopping_list,
            watchlist=watchlist_set_bench,
            scanner_symbols=scanner_symbols,
        )
    except Exception as e:
        log.warning("bench_build_failed", error=str(e))

    # 8d. LEAP radar — screen low-IV watchlist names, fetch real LEAP chains
    from datetime import date
    leap_candidates: list[LeapCandidate] = []
    try:
        tv_by_sym_lc: dict[str, str] = {}
        for ctx in intel_contexts:
            if ctx.technical_consensus:
                tv_by_sym_lc[ctx.symbol] = ctx.technical_consensus.overall

        for symbol, mkt, hist, chain, cal in watchlist_data:
            iv = mkt.iv_rank
            rsi = hist.rsi_14 or 50.0
            price = float(mkt.price) if mkt.price else 0
            tv = tv_by_sym_lc.get(symbol, "")

            if iv > 30:
                continue

            reasons: list[str] = []
            score = 0.0

            if rsi <= 35:
                score += 1
                reasons.append(f"RSI {rsi:.0f} — oversold")
            elif rsi <= 45:
                score += 0.5
                reasons.append(f"RSI {rsi:.0f} — pullback")

            sma200 = float(hist.sma_200) if hist.sma_200 else None
            sma50 = float(hist.sma_50) if hist.sma_50 else None
            if sma200 and price > 0:
                pct_from_200 = (price - sma200) / sma200
                if -0.05 <= pct_from_200 <= 0.02:
                    score += 1
                    reasons.append(f"At 200 SMA (${sma200:,.0f})")
            if sma50 and price > 0:
                pct_from_50 = (price - sma50) / sma50
                if -0.03 <= pct_from_50 <= 0.01:
                    score += 0.5
                    reasons.append(f"Near 50 SMA (${sma50:,.0f})")

            if tv in ("BUY", "STRONG_BUY"):
                score += 1
                reasons.append(f"TV {tv}")

            if hist.high_52w > 0 and hist.low_52w > 0:
                range_52w = float(hist.high_52w - hist.low_52w)
                if range_52w > 0:
                    pct_in_range = (price - float(hist.low_52w)) / range_52w
                    if pct_in_range < 0.30:
                        score += 1
                        reasons.append("Bottom 30% of 52w range")

            sl_entry = shopping_list_by_ticker.get(symbol)
            if sl_entry and sl_entry.rating_tier >= 3:
                score += 0.5
                reasons.append(f"Shopping list: {sl_entry.rating}")

            if score < 2:
                continue

            reasons.insert(0, f"IV rank {iv:.0f} — options are cheap")
            leap_candidates.append(LeapCandidate(
                symbol=symbol, price=price, iv_rank=iv, rsi=rsi, reasons=reasons,
            ))

        leap_candidates.sort(key=lambda x: x.iv_rank)
        leap_candidates = leap_candidates[:5]

        # Fetch real LEAP chain for each candidate
        for lc in leap_candidates:
            try:
                # Get expirations list from the existing chain
                _, _, _, existing_chain, _ = next(
                    (w for w in watchlist_data if w[0] == lc.symbol), (None,)*5
                )
                exps = existing_chain.expirations if existing_chain else []
                leap_exps = [e for e in exps
                             if (e - date.today()).days >= 270]
                if not leap_exps:
                    continue

                furthest = max(leap_exps)
                lc.expiration = furthest.strftime("%b %Y")
                lc.dte = (furthest - date.today()).days

                # Fetch chain at LEAP expiration
                leap_chain = None
                leap_dte = (furthest - date.today()).days
                if etrade_session:
                    try:
                        from src.data.broker import fetch_etrade_chain
                        leap_chain = fetch_etrade_chain(
                            etrade_session, lc.symbol, lc.price,
                            target_dte=leap_dte,
                        )
                    except Exception:
                        pass
                if not leap_chain or not leap_chain.calls:
                    leap_chain = fetch_options_chain(lc.symbol, target_dte=leap_dte)

                if leap_chain and leap_chain.calls:
                    # Find deep ITM call: strike 10-20% below current price
                    # Deep ITM means more intrinsic value (doesn't decay) vs time value (does)
                    has_delta = any(abs(c.delta) > 0.01 for c in leap_chain.calls)
                    target_strike = lc.price * 0.85  # 15% below current price
                    itm_calls = [c for c in leap_chain.calls
                                 if float(c.strike) <= lc.price * 0.95
                                 and float(c.bid) > 0]
                    if has_delta:
                        # Further filter: delta >= 0.65 ensures it tracks the stock
                        itm_calls = [c for c in itm_calls if abs(c.delta) >= 0.65]
                    if not itm_calls:
                        # Fallback: any ITM call with positive bid
                        itm_calls = [c for c in leap_chain.calls
                                     if float(c.strike) < lc.price
                                     and float(c.bid) > 0]
                    if not itm_calls:
                        continue
                    # Pick strike closest to 15% below current price
                    best = min(itm_calls,
                               key=lambda c: abs(float(c.strike) - target_strike))

                    if float(best.bid) <= 0:
                        continue

                    lc.strike = best.strike
                    lc.delta = best.delta if has_delta else None
                    lc.bid = float(best.bid)
                    lc.ask = float(best.ask)
                    lc.mid = float(best.mid)
                    lc.open_interest = best.open_interest
            except Exception as e:
                log.warning("leap_chain_fetch_failed", symbol=lc.symbol, error=str(e))
    except Exception as e:
        log.warning("leap_radar_failed", error=str(e))

    # 9. Build and print briefing
    briefing = format_local_briefing(
        regime=regime,
        vix=vix,
        spy_change=spy_change,
        all_signals=all_signals,
        watchlist_data=watchlist_data,
        tax_alerts=tax_alerts,
        recommendations=recommendations,
        intel_contexts=intel_contexts,
        analyst_brief=analyst_brief,
        position_reviews=position_reviews,
        tax_engine=tax_engine,
        portfolio_state=portfolio_state,
        scanner_picks=scanner_picks,
        bench=bench,
        leap_candidates=leap_candidates,
    )
    print(briefing)

    # Telegram disabled — running locally for now
    # tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    # tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    # if tg_token and tg_chat and always_push:
    #     try:
    #         plain = _strip_ansi(briefing)
    #         from src.delivery.telegram_bot import send_briefing
    #         await send_briefing(plain)
    #         log.info("telegram_briefing_sent", cycle=cycle_name)
    #     except Exception as e:
    #         log.warning("telegram_send_failed", error=str(e))

    result = {
        "cycle": cycle_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "regime": regime.regime,
        "signals_fired": len(all_signals),
        "liquidity_healthy": liquidity_ok,
        "tax_alerts": len(tax_alerts),
    }

    log.info("analysis_cycle_complete", **result)
    return result


async def run_morning_briefing() -> dict[str, Any]:
    """Run the full morning briefing (8:00 AM cycle)."""
    return await run_analysis_cycle("morning", always_push=True)


async def run_sentinel_check() -> dict[str, Any]:
    """Run pre-market sentinel check."""
    from src.monitor.sentinel import check_premarket, format_sentinel_alert

    log.info("sentinel_check_start")
    vix, spy_change = fetch_vix_and_spy()
    alert = check_premarket(
        spy_futures_pct=spy_change,
        nasdaq_futures_pct=spy_change,  # approximate
        vix_futures_change=vix - 20.0,
    )

    if alert.triggered:
        msg = format_sentinel_alert(alert)
        log.warning("sentinel_triggered", alert=msg)
        print(msg)

    return {
        "triggered": alert.triggered,
        "severity": alert.severity,
    }


# ---------------------------------------------------------------------------
# Run modes
# ---------------------------------------------------------------------------

async def mode_briefing() -> None:
    """Single morning briefing run."""
    await run_morning_briefing()


async def mode_paper() -> None:
    """Paper trading mode — runs signals then submits orders to Alpaca.

    Uses real Alpaca paper API if ALPACA_API_KEY is set, otherwise
    in-memory simulation. Either way, the full signal + sizing pipeline
    runs on live yfinance data.
    """
    from datetime import date as date_type, timedelta

    from src.analysis.sizing import size_position
    from src.analysis.strikes import find_smart_strikes
    from src.execution.alpaca_client import AlpacaPaperClient
    from src.models.position import PortfolioState

    log.info("mode_paper_start")

    # 1. Connect to Alpaca and clean stale state
    alpaca = AlpacaPaperClient()
    if alpaca.is_live:
        alpaca.cancel_all_orders()
        log.info("cancelled_stale_orders")
    acct = alpaca.get_account()
    log.info("alpaca_account",
             mode="LIVE API" if alpaca.is_live else "IN-MEMORY SIM",
             equity=str(acct.equity), cash=str(acct.cash),
             buying_power=str(acct.buying_power))

    # 2. Run the full briefing pipeline (data + signals)
    result = await run_analysis_cycle("paper_scan", always_push=True)

    # 3. Re-fetch signals for order submission (briefing already printed them)
    vix, spy_change = fetch_vix_and_spy()
    symbols = load_watchlist()
    watchlist_data = fetch_all_watchlist_data(symbols)

    all_signals: list[AlphaSignal] = []
    for symbol, mkt, hist, chain, cal in watchlist_data:
        sigs = detect_all_signals(symbol, mkt, hist, chain, cal)
        all_signals.extend(sigs)

    if not all_signals:
        print("\nNo signals fired — nothing to trade today.")
        _print_alpaca_dashboard(alpaca)
        return

    # 4. Group signals by symbol and size positions
    from collections import defaultdict
    by_symbol: dict[str, list[AlphaSignal]] = defaultdict(list)
    for s in all_signals:
        by_symbol[s.symbol].append(s)

    # Build a stub portfolio state from Alpaca account
    portfolio = PortfolioState(
        net_liquidation=acct.equity,
        cash_available=acct.cash,
        buying_power=acct.buying_power,
    )

    # Price data lookup
    price_data = {sym: (mkt, hist, chain) for sym, mkt, hist, chain, _ in watchlist_data}

    print(f"\n{'=' * 60}")
    print(f"  PAPER TRADING — ORDER PROPOSALS")
    print(f"{'=' * 60}")

    orders_submitted = 0
    for symbol, sigs in sorted(by_symbol.items(), key=lambda x: -max(s.strength for s in x[1])):
        if symbol not in price_data:
            continue

        mkt, hist, chain = price_data[symbol]

        # Find best strike at a technical level
        strikes = find_smart_strikes(symbol, chain, hist, "sell_put")
        if not strikes:
            log.info("no_strikes_found", symbol=symbol)
            print(f"\n  {symbol}: signals fired but no valid strikes found")
            continue

        best_strike = strikes[0]

        # Pick expiration ~30 DTE
        target_exp = date_type.today() + timedelta(days=30)

        # Size the position
        sized = size_position(
            symbol=symbol,
            trade_type="sell_put",
            strike=best_strike,
            expiration=target_exp,
            signals=sigs,
            portfolio=portfolio,
        )

        if sized.contracts == 0:
            print(f"\n  {symbol}: can't afford 1 contract at ${best_strike.strike} "
                  f"({sized.conviction} conviction) — skipping")
            continue

        # Print proposal
        sig_names = ", ".join(s.signal_type.value for s in sigs)
        print(f"\n  {symbol} — {sized.conviction.upper()} conviction")
        print(f"    Signals: {sig_names}")
        print(f"    Strike:  ${best_strike.strike} P @ ${best_strike.premium}")
        print(f"    Size:    {sized.contracts}x ({sized.portfolio_pct:.1%} of NLV)")
        print(f"    Yield:   {best_strike.annualized_yield:.0%} annualized")
        if best_strike.technical_reason:
            print(f"    Level:   {best_strike.technical_reason}")

        # Submit to Alpaca (paper)
        try:
            order = alpaca.sell_to_open_option(
                underlying=symbol,
                expiration=target_exp,
                option_type="put",
                strike=best_strike.strike,
                quantity=sized.contracts,
                limit_price=best_strike.premium,
            )
            print(f"    Order:   {order.order_id} — {order.status}")
            orders_submitted += 1
        except Exception as e:
            print(f"    Order FAILED: {e}")
            log.warning("order_failed", symbol=symbol, error=str(e))

    print(f"\n  Orders submitted: {orders_submitted}")
    print(f"{'=' * 60}")

    # 5. Print account dashboard
    _print_alpaca_dashboard(alpaca)


def _print_alpaca_dashboard(alpaca: Any) -> None:
    """Print Alpaca account status."""
    acct = alpaca.get_account()
    positions = alpaca.get_positions()
    orders = alpaca.get_order_history(limit=10)

    print(f"\n{'=' * 60}")
    print(f"  ALPACA PAPER ACCOUNT")
    print(f"  {'Live API' if alpaca.is_live else 'In-Memory Simulation'}")
    print(f"{'=' * 60}")
    print(f"  Equity:       ${acct.equity:,.2f}")
    print(f"  Cash:         ${acct.cash:,.2f}")
    print(f"  Buying Power: ${acct.buying_power:,.2f}")

    if positions:
        print(f"\n  POSITIONS ({len(positions)}):")
        for p in positions:
            print(f"    {p.symbol}: {p.quantity}x @ ${p.avg_entry_price} "
                  f"(P&L: ${p.unrealized_pnl:,.2f})")
    else:
        print(f"\n  No open positions.")

    filled = [o for o in orders if o.status == "filled"]
    if filled:
        print(f"\n  RECENT FILLS ({len(filled)}):")
        for o in filled[:5]:
            print(f"    {o.order_id}: {o.position_intent} {o.symbol} "
                  f"{o.quantity}x @ ${o.filled_price}")

    print(f"{'=' * 60}")


async def mode_backtest() -> None:
    """Run walk-forward backtests on all signals."""
    from src.backtest import WalkForwardConfig, format_backtest_summary, run_walk_forward

    log.info("mode_backtest_start")
    config = WalkForwardConfig(train_window=150, test_window=75, step_size=50)

    results = []
    for signal in ["multi_day_pullback", "iv_rank_spike", "oversold_rsi"]:
        result = run_walk_forward(signal, {}, config)
        results.append(result)

    summary = format_backtest_summary(results)
    log.info("backtest_complete", signals_tested=len(results))
    print(summary)


async def mode_onboard() -> None:
    """First-time onboarding flow."""
    log.info("mode_onboard_start")
    log.info("onboard_complete", message="Interactive onboarding via Telegram")


async def mode_weekend_review() -> None:
    """Saturday learning loop + weekly review."""
    from src.learning import format_learning_report, run_weekly_review

    log.info("mode_weekend_review_start")
    report = run_weekly_review([], {})
    output = format_learning_report(report)
    log.info("weekend_review_complete")
    print(output)


async def mode_daemon() -> None:
    """Long-running daemon: renew tokens every hour, run briefings on schedule.

    Default schedule: morning (8:00 AM ET) + post-market (4:30 PM ET).
    Token renewal every 60 minutes keeps E*Trade session alive until midnight ET.
    """
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    # Which cycles to run full briefings (always_push ones by default)
    briefing_cycles = [c for c in ANALYSIS_CYCLES if c["always_push"]]
    cycle_times = {c["time"] for c in briefing_cycles}
    cycle_names = {c["time"]: c["name"] for c in briefing_cycles}

    log.info("daemon_start",
             briefing_times=[t.strftime("%H:%M") for t in sorted(cycle_times)],
             token_renewal="every 60 min")

    last_renewal = datetime.min
    ran_today: set[str] = set()  # cycle names already run today

    while True:
        now_et = datetime.now(et)
        today_str = now_et.strftime("%Y-%m-%d")

        # Reset ran_today at midnight
        if not any(today_str in s for s in ran_today):
            ran_today.clear()

        # Token renewal every 60 minutes
        if (datetime.now() - last_renewal).total_seconds() > 3600:
            try:
                from src.data.auth import load_tokens, renew_tokens
                saved = load_tokens()
                if saved:
                    ok = renew_tokens(str(saved["oauth_token"]), str(saved["oauth_secret"]))
                    if ok:
                        log.info("daemon_token_renewed")
                    else:
                        log.warning("daemon_token_renewal_failed")
                last_renewal = datetime.now()
            except Exception as e:
                log.warning("daemon_renewal_error", error=str(e))

        # Check if it's time for a briefing
        for cycle_time in sorted(cycle_times):
            key = f"{today_str}:{cycle_names[cycle_time]}"
            if key in ran_today:
                continue
            # Run if we're within 5 minutes after the scheduled time
            scheduled = now_et.replace(
                hour=cycle_time.hour, minute=cycle_time.minute, second=0,
            )
            delta = (now_et - scheduled).total_seconds()
            if 0 <= delta <= 300:  # within 5 min window
                log.info("daemon_briefing_start", cycle=cycle_names[cycle_time])
                try:
                    await run_analysis_cycle(
                        cycle_names[cycle_time], always_push=True,
                    )
                except Exception as e:
                    log.error("daemon_briefing_failed", error=str(e))
                ran_today.add(key)

        # Sleep 60 seconds before next check
        await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

RUN_MODES = {
    "briefing": mode_briefing,
    "paper": mode_paper,
    "backtest": mode_backtest,
    "onboard": mode_onboard,
    "weekend-review": mode_weekend_review,
    "daemon": mode_daemon,
}


def main() -> None:
    """Parse args and run the appropriate mode."""
    parser = argparse.ArgumentParser(description="Wheel Copilot")
    parser.add_argument(
        "--mode",
        choices=list(RUN_MODES.keys()),
        default="daemon",
        help="Run mode (default: daemon)",
    )
    args = parser.parse_args()

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(20),
    )

    log.info("wheel_copilot_start", mode=args.mode)
    handler = RUN_MODES[args.mode]
    asyncio.run(handler())


if __name__ == "__main__":
    main()
