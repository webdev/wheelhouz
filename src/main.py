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
# Recommendation engine — turns signals into sized trade proposals
# ---------------------------------------------------------------------------

def build_recommendations(
    all_signals: list[AlphaSignal],
    watchlist_data: list[tuple[str, MarketContext, PriceHistory, OptionsChain, EventCalendar]],
    portfolio: PortfolioState | None = None,
    intel_contexts: list[IntelligenceContext] | None = None,
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
) -> str:
    """Format an action-oriented briefing. Structure: DO NOW → CONSIDER → WATCH → MARKET."""
    from datetime import date, timedelta

    today = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    regime_tag = {"attack": "ATTACK", "hold": "HOLD", "defend": "DEFEND", "crisis": "CRISIS"}
    lines: list[str] = []

    # Header — compact, regime baked in
    regime_name = regime_tag.get(regime.regime, regime.regime.upper())
    regime_colors = {"ATTACK": _C.green, "HOLD": _C.yellow, "DEFEND": _C.red, "CRISIS": _C.red}
    regime_fn = regime_colors.get(regime_name, _C.yellow)
    spy_fn = _C.green if spy_change >= 0 else _C.red

    lines.append(_C.dim(f"{'=' * 60}"))
    lines.append(_C.bold(f"  WHEEL COPILOT — {today}"))
    lines.append(f"  {regime_fn(regime_name)} "
                 f"| VIX {vix:.1f} | SPY {spy_fn(f'{spy_change:+.2%}')}")
    if portfolio_state and portfolio_state.net_liquidation > 0:
        nlv = portfolio_state.net_liquidation
        cash = portfolio_state.cash_available
        deployed_pct = float((nlv - cash) / nlv) if nlv else 0
        lines.append(f"  NLV: {_C.bold(f'${nlv:,.0f}')} "
                     f"| Cash: {_C.green(f'${cash:,.0f}')} "
                     f"| Deployed: {deployed_pct:.0%}")
    if tax_engine:
        net_ytd = (tax_engine.realized_stcg_ytd + tax_engine.realized_ltcg_ytd
                   - tax_engine.realized_losses_ytd)
        net_fn = _C.green if net_ytd >= 0 else _C.red
        lines.append(f"  YTD Options P&L: {net_fn(f'${net_ytd:+,.0f}')} "
                     f"| Premium: {_C.green(f'${tax_engine.option_premium_income_ytd:,.0f}')}")
    lines.append(_C.dim(f"{'=' * 60}"))

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
        lines.append(f"\n{_C.red(_C.bold('━━ DO NOW ━━'))}")

        for p in urgent_positions:
            pos_desc = _format_position_desc(p)
            action_color = _C.red if p.action == "CLOSE NOW" else _C.green
            lines.append(f"  {action_color(p.action)}: {_C.bold(pos_desc)}")
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
            exp_str = r.expiration.strftime("%b %d") if r.expiration else "~30 DTE"
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

    if medium_trades or low_trades:
        lines.append(f"\n{_C.blue(_C.bold('━━ CONSIDER ━━'))}")

        for r in medium_trades + low_trades:
            exp_str = r.expiration.strftime("%b %d") if r.expiration else "~30 DTE"
            delta_str = f" | delta {r.smart_strike.delta:.2f}" if r.smart_strike else ""
            tv_str = ""
            if intel_contexts:
                ctx_match = next((c for c in intel_contexts if c.symbol == r.symbol), None)
                if ctx_match and ctx_match.technical_consensus:
                    tv_str = f" | TV {ctx_match.technical_consensus.overall}"

            is_call = r.trade_type == "sell_call"
            opt_type = "Call" if is_call else "Put"
            lines.append(f"   >> {_C.bold(r.symbol)} — Sell ${r.strike} {opt_type} exp {exp_str}")
            lines.append(
                f"      {r.contracts}x @ ${r.premium} mid | "
                f"{r.annualized_yield:.0%} ann.{delta_str}{tv_str}"
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
            lines.append(f"      {r.reasoning}")

    # ── OPPORTUNITIES — proactive deployment recommendations ──
    # Evaluate watchlist names NOT already covered by signal-driven recommendations
    rec_symbols: set[str] = set()
    if recommendations:
        rec_symbols.update(r.symbol for r in recommendations)

    # (symbol, type, reason, details, option_contract_or_None)
    opportunities: list[tuple[str, str, str, list[str], "OptionContract | None"]] = []

    cash = portfolio_state.cash_available if portfolio_state else Decimal("0")
    nlv = portfolio_state.net_liquidation if portfolio_state else Decimal("0")

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

        # Skip if earnings within 14 days (don't open new through earnings)
        if cal.next_earnings and cal.next_earnings <= date.today() + timedelta(days=14):
            continue

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
            # Target: 0.20-0.30 delta, 30-45 DTE
            today = date.today()
            candidates = [
                p for p in chain.puts
                if 0.10 <= abs(p.delta) <= 0.35
                and 20 <= (p.expiration - today).days <= 55
                and p.bid > 0
            ]
            if candidates:
                # Prefer closest to 0.25 delta in the 30-45 DTE range
                best_put = min(
                    candidates,
                    key=lambda p: (abs(abs(p.delta) - 0.25) + abs((p.expiration - today).days - 37) / 100)
                )

        if rec_type:
            opportunities.append((symbol, rec_type, reason, details, best_put))

    # Sort by detail count (proxy for conviction) descending
    opportunities.sort(key=lambda x: len(x[3]), reverse=True)

    # ── Reallocation candidates: underperforming stock that could be redeployed ──
    realloc_candidates: list[tuple[str, int, Decimal, Decimal, float, str]] = []
    # (symbol, shares, cost_basis, current_price, pnl_pct, reason)
    if portfolio_state and portfolio_state.positions:
        for pos in portfolio_state.positions:
            if pos.position_type != "long_stock" or pos.quantity < 1:
                continue
            if pos.underlying_price <= 0:
                continue
            # cost_basis may be total (all shares) or per-share — normalize
            if pos.cost_basis <= 0:
                continue
            per_share_cost = pos.cost_basis
            if pos.quantity > 1 and per_share_cost > pos.underlying_price * 3:
                # Likely total cost basis, not per-share — normalize
                per_share_cost = pos.cost_basis / pos.quantity
            if per_share_cost <= 0:
                continue
            pnl_pct = float((pos.underlying_price - per_share_cost) / per_share_cost)
            sym_tv = tv_by_sym.get(pos.symbol, "")

            # Find underperformers: down significantly OR bearish consensus
            reason_parts: list[str] = []
            if pnl_pct < -0.15:
                reason_parts.append(f"down {pnl_pct:.0%} from cost basis")
            elif pnl_pct < -0.05 and sym_tv in ("SELL", "STRONG_SELL"):
                reason_parts.append(f"down {pnl_pct:.0%}, TV {sym_tv}")
            # Small position that can't run the wheel (< 100 shares, not near 100)
            if pos.quantity < 100 and pos.quantity < 90:
                reason_parts.append(f"only {pos.quantity} shares — can't sell covered calls")

            if reason_parts:
                realloc_candidates.append((
                    pos.symbol, pos.quantity, per_share_cost,
                    pos.underlying_price, pnl_pct, " | ".join(reason_parts),
                ))

    # Sort by worst performer first
    realloc_candidates.sort(key=lambda x: x[4])

    if opportunities and cash > 0:
        lines.append(f"\n{_C.green(_C.bold('━━ OPPORTUNITIES ━━'))} "
                     f"{_C.dim(f'— ${cash:,.0f} cash available')}")

        for symbol, rec_type, reason, details, put_contract in opportunities[:8]:
            _, mkt, hist, _, _ = next(
                (w for w in watchlist_data if w[0] == symbol), (None,)*5
            )
            price = float(mkt.price) if mkt else 0

            type_fn = _C.green if rec_type == "BUY 100 SHARES" else _C.cyan
            lines.append(f"  {type_fn(rec_type)}: {_C.bold(symbol)} @ ${price:,.2f}")
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
                ann_yield = yield_on_cap * (365 / dte) if dte > 0 else 0
                lines.append(
                    f"    {_C.bold('Strike')}: ${put_contract.strike} "
                    f"| {_C.bold('Exp')}: {put_contract.expiration.strftime('%b %d')} ({dte}d) "
                    f"| {_C.bold('Bid')}: ${bid:.2f} "
                    f"| {_C.bold('Delta')}: {abs(put_contract.delta):.2f}"
                )
                lines.append(
                    f"    Premium: ${mid:.2f}/contract "
                    f"| Yield: {yield_on_cap:.1f}% ({ann_yield:.0f}% ann)"
                )

            # Sizing: 1.5% NLV target, hard cap at 5% NLV
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
                    collateral = contracts * price * 100
                    lines.append(f"    Size: ~{contracts}x puts (${collateral:,.0f} collateral, "
                                 f"~{collateral / float(nlv):.1%} NLV)")

    # ── REALLOCATE — underperforming positions to redeploy ──
    if realloc_candidates:
        lines.append(f"\n{_C.yellow(_C.bold('━━ REALLOCATE ━━'))} "
                     f"{_C.dim('— sell underperformers, redeploy into wheel')}")
        for sym, qty, basis, cur_price, pnl_pct, reason in realloc_candidates[:5]:
            value = qty * cur_price
            pnl_dollar = qty * (cur_price - basis)
            pnl_color = _C.red if pnl_pct < 0 else _C.green
            lines.append(
                f"  {_C.yellow('SELL')}: {_C.bold(sym)} — {qty} shares @ ${cur_price:,.2f} "
                f"({pnl_color(f'{pnl_pct:+.0%}')}, {pnl_color(f'${pnl_dollar:+,.0f}')})"
            )
            lines.append(f"    {reason}")
            lines.append(f"    Frees ${value:,.0f} — redeploy via puts on stronger names")

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
        lines.append(f"\n{_C.yellow(_C.bold('━━ WATCH ━━'))}")

        for p in watch_positions:
            pos_desc = _format_position_desc(p)
            action_color = _C.yellow if p.action == "WATCH CLOSELY" else _C.dim
            lines.append(f"  {_C.bold(pos_desc)} — {action_color(p.action)}")
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
                lines.append(f"  Earnings: {sym} {dt} ({label})")

        for alert in tax_alerts:
            lines.append(f"  Tax: {alert}")

    # Concentration warnings (cross-position)
    if position_reviews:
        from collections import Counter
        sym_counts = Counter(p.symbol for p in position_reviews)
        concentrated = [(sym, cnt) for sym, cnt in sym_counts.items() if cnt >= 2]
        if concentrated:
            if not has_watch:
                lines.append(f"\n{_C.yellow(_C.bold('━━ WATCH ━━'))}")
            lines.append("")
            for sym, cnt in sorted(concentrated, key=lambda x: -x[1]):
                sym_positions = [p for p in position_reviews if p.symbol == sym]
                total_exposure = sum(abs(p.current_pnl) + abs(p.entry_price * 100 * p.quantity)
                                     for p in sym_positions)
                lines.append(f"  {_C.yellow('!!')} {_C.bold(sym)}: {cnt} open option positions — "
                             f"watch single-name concentration (max 10% NLV)")

    # ── Nothing to do ──
    if not (urgent_positions or high_trades or medium_trades or low_trades
            or watch_positions):
        lines.append(f"\n  {_C.green('No signals fired. Sit tight.')}")

    # ── ANALYST BRIEF — Claude reasoning (when available) ──
    if analyst_brief:
        lines.append(f"\n{_C.cyan(_C.bold('━━ ANALYST BRIEF ━━'))}")
        lines.append(analyst_brief)

    # ── YTD P&L — realized option performance from E*Trade ──
    if tax_engine and (tax_engine.option_premium_income_ytd > 0
                       or tax_engine.realized_stcg_ytd > 0
                       or tax_engine.realized_losses_ytd > 0):
        lines.append(f"\n{_C.blue(_C.bold('━━ YTD OPTIONS P&L ━━'))}")
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
        lines.append(f"\n{_C.dim('━━ SKIP ━━')}")
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
            for pos in portfolio_state.positions:
                # Only review option positions — stocks need different logic
                if not pos.option_type:
                    continue
                # Find matching intelligence context
                matching_ctx = next(
                    (c for c in intel_contexts if c.symbol == pos.symbol), None
                )
                if matching_ctx:
                    chain = chain_by_symbol.get(pos.symbol)
                    review = review_position(pos, matching_ctx, chain=chain)
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
    )
    print(briefing)

    # Push to Telegram if configured
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if tg_token and tg_chat and always_push:
        try:
            plain = _strip_ansi(briefing)
            from src.delivery.telegram_bot import send_briefing
            await send_briefing(plain)
            log.info("telegram_briefing_sent", cycle=cycle_name)
        except Exception as e:
            log.warning("telegram_send_failed", error=str(e))

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
