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
from datetime import datetime, time, timezone
from decimal import Decimal
from typing import Any

import structlog

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

log = structlog.get_logger()


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
) -> list[tuple[str, MarketContext, PriceHistory, OptionsChain, EventCalendar]]:
    """Fetch real market data for all watchlist symbols via yfinance."""
    results = []
    for symbol in symbols:
        log.info("fetching_data", symbol=symbol)
        try:
            mkt = fetch_market_context(symbol)
            hist = fetch_price_history(symbol)
            chain = fetch_options_chain(symbol)
            cal = fetch_event_calendar(symbol)
            results.append((symbol, mkt, hist, chain, cal))
        except Exception as e:
            log.warning("symbol_fetch_failed", symbol=symbol, error=str(e))
    return results


# ---------------------------------------------------------------------------
# Recommendation engine — turns signals into sized trade proposals
# ---------------------------------------------------------------------------

def build_recommendations(
    all_signals: list[AlphaSignal],
    watchlist_data: list[tuple[str, MarketContext, PriceHistory, OptionsChain, EventCalendar]],
    portfolio: PortfolioState | None = None,
    intel_contexts: list[IntelligenceContext] | None = None,
) -> list[SizedOpportunity]:
    """Run strikes + sizing on every symbol with fired signals.

    TradingView consensus adjusts conviction after sizing:
    - SELL/STRONG_SELL → downgrade one level (can reach SKIP → filtered out)
    - BUY/STRONG_BUY → upgrade one level (capped at HIGH)
    - NEUTRAL → no change

    Returns a list of SizedOpportunity sorted by conviction then yield.
    """
    from collections import defaultdict
    from datetime import date as date_type, timedelta

    if portfolio is None:
        portfolio = PortfolioState()  # NLV=0 → sizing falls back to $1M

    by_symbol: dict[str, list[AlphaSignal]] = defaultdict(list)
    for s in all_signals:
        by_symbol[s.symbol].append(s)

    # Index TradingView consensus by symbol
    tv_by_symbol: dict[str, str] = {}
    if intel_contexts:
        for ctx in intel_contexts:
            if ctx.technical_consensus:
                tv_by_symbol[ctx.symbol] = ctx.technical_consensus.overall

    price_data = {sym: (mkt, hist, chain) for sym, mkt, hist, chain, _ in watchlist_data}
    target_exp = date_type.today() + timedelta(days=30)

    recommendations: list[SizedOpportunity] = []
    sized_symbols: set[str] = set()

    # 1. Signal-driven recommendations (quant signals fired)
    for symbol, sigs in by_symbol.items():
        if symbol not in price_data:
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

        if sized.conviction != "skip" and sized.contracts > 0:
            recommendations.append(sized)
            sized_symbols.add(symbol)

    # 2. TV-driven recommendations (no quant signals, but strong TV + IV)
    # These are valid wheel candidates on green days when dip signals don't fire.
    if intel_contexts:
        for ctx in intel_contexts:
            if ctx.symbol in sized_symbols or ctx.symbol not in price_data:
                continue
            tc = ctx.technical_consensus
            if not tc or tc.overall not in ("BUY", "STRONG_BUY"):
                continue
            if ctx.quant.iv_rank < 30:
                continue

            _, hist, chain = price_data[ctx.symbol]
            strikes = find_smart_strikes(ctx.symbol, chain, hist, "sell_put")
            if not strikes:
                continue

            # Size at LOW conviction (no quant signals to boost it)
            from src.models.enums import SignalType
            from datetime import datetime as dt_type
            tv_signal = AlphaSignal(
                symbol=ctx.symbol,
                signal_type=SignalType.IV_RANK_SPIKE,
                strength=0.5,
                direction="sell_put",
                reasoning=f"TV {tc.overall}, IV rank {ctx.quant.iv_rank:.0f}",
                expires=dt_type.now() + timedelta(hours=24),
            )
            sized = size_position(
                symbol=ctx.symbol,
                trade_type="sell_put",
                strike=strikes[0],
                expiration=target_exp,
                signals=[tv_signal],
                portfolio=portfolio,
            )
            # Force LOW conviction — TV alone isn't enough for MEDIUM/HIGH
            sized.conviction = "low"
            sized.reasoning = (
                f"TV {tc.overall} + IV rank {ctx.quant.iv_rank:.0f} "
                f"(no quant signals — sell on strength, not on dip)"
            )

            if sized.contracts > 0:
                recommendations.append(sized)

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


# ---------------------------------------------------------------------------
# Local briefing formatter (no Claude API needed)
# ---------------------------------------------------------------------------

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
) -> str:
    """Format an action-oriented briefing. Structure: DO NOW → CONSIDER → WATCH → MARKET."""
    from datetime import date, timedelta

    today = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    regime_tag = {"attack": "ATTACK", "hold": "HOLD", "defend": "DEFEND", "crisis": "CRISIS"}
    lines: list[str] = []

    # Header — compact, regime baked in
    lines.append(f"{'=' * 60}")
    lines.append(f"  WHEEL COPILOT — {today}")
    lines.append(f"  {regime_tag.get(regime.regime, regime.regime.upper())} "
                 f"| VIX {vix:.1f} | SPY {spy_change:+.2%}")
    lines.append(f"{'=' * 60}")

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
        lines.append(f"\n━━ DO NOW ━━")

        for p in urgent_positions:
            pos_desc = _format_position_desc(p)
            pnl_str = f"+${p.current_pnl:,.0f}" if p.current_pnl >= 0 else f"-${abs(p.current_pnl):,.0f}"
            lines.append(f"  {p.action}: {pos_desc}")
            lines.append(f"    P&L: {pnl_str} ({p.pnl_pct:+.0%}) | {p.days_to_expiry}d left")
            lines.append(f"    {p.reasoning}")

        for r in high_trades:
            exp_str = r.expiration.strftime("%b %d") if r.expiration else "~30 DTE"
            delta_str = f" | delta {r.smart_strike.delta:.2f}" if r.smart_strike else ""
            tv_str = ""
            if intel_contexts:
                ctx_match = next((c for c in intel_contexts if c.symbol == r.symbol), None)
                if ctx_match and ctx_match.technical_consensus:
                    tv_str = f" | TV {ctx_match.technical_consensus.overall}"

            lines.append(f"  >>> {r.symbol} — Sell Cash-Secured Put")
            lines.append(
                f"      {r.contracts}x ${r.strike} P exp {exp_str} "
                f"@ ${r.premium} mid"
            )
            lines.append(
                f"      {r.annualized_yield:.0%} ann. yield{delta_str}{tv_str}"
            )
            lines.append(
                f"      Collateral ${r.capital_deployed:,.0f} "
                f"({r.portfolio_pct:.1%} NLV) | "
                f"Max profit ${r.premium * r.contracts * 100:,.0f}"
            )
            sig_names = ", ".join(s.signal_type.value for s in r.signals)
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

    if medium_trades or low_trades:
        lines.append(f"\n━━ CONSIDER ━━")

        for r in medium_trades + low_trades:
            exp_str = r.expiration.strftime("%b %d") if r.expiration else "~30 DTE"
            delta_str = f" | delta {r.smart_strike.delta:.2f}" if r.smart_strike else ""
            tv_str = ""
            if intel_contexts:
                ctx_match = next((c for c in intel_contexts if c.symbol == r.symbol), None)
                if ctx_match and ctx_match.technical_consensus:
                    tv_str = f" | TV {ctx_match.technical_consensus.overall}"

            lines.append(f"   >> {r.symbol} — Sell ${r.strike} Put exp {exp_str}")
            lines.append(
                f"      {r.contracts}x @ ${r.premium} mid | "
                f"{r.annualized_yield:.0%} ann.{delta_str}{tv_str}"
            )
            lines.append(
                f"      Collateral ${r.capital_deployed:,.0f} "
                f"({r.portfolio_pct:.1%} NLV) | "
                f"Max profit ${r.premium * r.contracts * 100:,.0f}"
            )
            lines.append(f"      {r.reasoning}")

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
        lines.append(f"\n━━ WATCH ━━")

        for p in watch_positions:
            pos_desc = _format_position_desc(p)
            pnl_str = f"+${p.current_pnl:,.0f}" if p.current_pnl >= 0 else f"-${abs(p.current_pnl):,.0f}"
            lines.append(f"  {pos_desc} — {p.action}")
            lines.append(f"    P&L: {pnl_str} ({p.pnl_pct:+.0%}) | {p.days_to_expiry}d left | {p.reasoning}")

        if upcoming_earnings:
            lines.append("")
            for sym, dt, days in sorted(upcoming_earnings, key=lambda x: x[2]):
                label = "TOMORROW" if days <= 1 else f"{days}d"
                lines.append(f"  Earnings: {sym} {dt} ({label})")

        for alert in tax_alerts:
            lines.append(f"  Tax: {alert}")

    # ── Nothing to do ──
    if not (urgent_positions or high_trades or medium_trades or low_trades
            or watch_positions):
        lines.append(f"\n  No signals fired. Sit tight.")

    # ── ANALYST BRIEF — Claude reasoning (when available) ──
    if analyst_brief:
        lines.append(f"\n━━ ANALYST BRIEF ━━")
        lines.append(analyst_brief)

    # ── SKIP — names the system looked at and explicitly rejected ──
    # Collect symbols already covered in DO NOW / CONSIDER / WATCH
    covered: set[str] = set()
    if recommendations:
        covered.update(r.symbol for r in recommendations)
    if position_reviews:
        covered.update(p.symbol for p in position_reviews)
    # TV-driven opportunities are now in recommendations, so they're already covered

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
        lines.append(f"\n━━ SKIP ━━")
        for s in skips:
            lines.append(s)

    lines.append(f"\n{'=' * 60}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------

async def run_analysis_cycle(
    cycle_name: str,
    always_push: bool = False,
) -> dict[str, Any]:
    """Run a single analysis cycle with live yfinance data.

    Pipeline: data -> signals -> regime -> risk -> briefing.
    """
    log.info("analysis_cycle_start", cycle=cycle_name)

    # 1. Macro data
    vix, spy_change = fetch_vix_and_spy()
    log.info("macro_data", vix=round(vix, 2), spy_change=round(spy_change, 4))

    # 2. Regime classification
    regime = classify_regime(vix, spy_change)
    log.info("regime_classified", regime=regime.regime, reason=regime.severity)

    # 3. Fetch per-symbol data
    symbols = load_watchlist()
    log.info("fetching_watchlist", symbols=len(symbols))
    watchlist_data = fetch_all_watchlist_data(symbols)
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
    portfolio_state = None
    position_reviews = []
    try:
        portfolio_state = load_portfolio_state()
        if portfolio_state.positions:
            for pos in portfolio_state.positions:
                # Only review option positions — stocks need different logic
                if not pos.option_type:
                    continue
                # Find matching intelligence context
                matching_ctx = next(
                    (c for c in intel_contexts if c.symbol == pos.symbol), None
                )
                if matching_ctx:
                    review = review_position(pos, matching_ctx)
                    position_reviews.append(review)
    except Exception as e:
        log.warning("portfolio_load_skipped", error=str(e))

    # 6. Claude analyst brief (opt-in, requires ANTHROPIC_API_KEY)
    analyst_brief = None
    contexts_with_signals = [c for c in intel_contexts if c.quant.signal_count > 0]
    if contexts_with_signals:
        regime_str = f"{regime.regime.upper()} — VIX {vix:.1f}, SPY {spy_change:+.2%}"
        analyst_brief = await generate_analyst_brief(contexts_with_signals, regime_str)

    # 7. Build sized recommendations from signals (TV adjusts conviction)
    recommendations = build_recommendations(all_signals, watchlist_data, intel_contexts=intel_contexts)

    # 8. Risk checks
    router = AccountRouter()
    liquidity_ok, liquidity_msg = check_liquidity_health(router)
    tax_alerts = generate_tax_alerts([])

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
    )
    print(briefing)

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
    """Full daemon mode — 5x daily analysis + continuous monitor + sentinel."""
    log.info("daemon_start", cycles=len(ANALYSIS_CYCLES))
    await run_morning_briefing()
    log.info("daemon_running", message="Continuous monitor would start here")


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
