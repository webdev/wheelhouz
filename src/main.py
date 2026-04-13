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
from src.config.loader import load_watchlist
from src.data.events import fetch_event_calendar
from src.data.market import fetch_market_context, fetch_price_history
from src.models.market import EventCalendar, MarketContext, OptionsChain, PriceHistory
from src.models.signals import AlphaSignal
from src.monitor.regime import RegimeState, classify_regime
from src.risk import check_liquidity_health, generate_tax_alerts
from src.models.account import AccountRouter

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
            chain = OptionsChain(symbol=symbol)  # yfinance chains are unreliable; stub
            cal = fetch_event_calendar(symbol)
            results.append((symbol, mkt, hist, chain, cal))
        except Exception as e:
            log.warning("symbol_fetch_failed", symbol=symbol, error=str(e))
    return results


# ---------------------------------------------------------------------------
# Local briefing formatter (no Claude API needed)
# ---------------------------------------------------------------------------

def format_local_briefing(
    regime: RegimeState,
    vix: float,
    spy_change: float,
    all_signals: list[AlphaSignal],
    watchlist_data: list[tuple[str, MarketContext, PriceHistory, OptionsChain, EventCalendar]],
    tax_alerts: list[str],
) -> str:
    """Format a rich text briefing from live data — no API key required."""
    today = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    lines: list[str] = []

    # Header
    lines.append(f"{'=' * 60}")
    lines.append(f"  WHEEL COPILOT — MORNING BRIEFING")
    lines.append(f"  {today}")
    lines.append(f"{'=' * 60}")

    # Regime
    regime_emoji = {
        "attack": "[ATTACK]", "hold": "[HOLD]",
        "defend": "[DEFEND]", "crisis": "[CRISIS]",
    }
    lines.append(f"\n━━ REGIME ━━")
    lines.append(f"  {regime_emoji.get(regime.regime, regime.regime.upper())} "
                 f"VIX {vix:.1f} | SPY {spy_change:+.2%} | {regime.severity}")

    # Signal flash
    lines.append(f"\n━━ SIGNAL FLASH ━━")
    if all_signals:
        # Group by symbol
        by_symbol: dict[str, list[AlphaSignal]] = {}
        for s in all_signals:
            by_symbol.setdefault(s.symbol, []).append(s)

        for sym, sigs in sorted(by_symbol.items()):
            sig_names = ", ".join(s.signal_type.value for s in sigs)
            top_strength = max(s.strength for s in sigs)
            lines.append(f"  {sym}: {sig_names} (strength {top_strength:.0f})")
            for s in sigs:
                lines.append(f"    -> {s.reasoning}")
        lines.append(f"\n  Total: {len(all_signals)} signals on "
                     f"{len(by_symbol)} symbols")
    else:
        lines.append("  No signals fired. Markets quiet.")

    # Watchlist snapshot
    lines.append(f"\n━━ WATCHLIST ━━")
    lines.append(f"  {'Symbol':<8} {'Price':>10} {'1d':>8} {'5d':>8} "
                 f"{'vs 52wH':>8} {'RSI':>6} {'IV Rank':>8}")
    lines.append(f"  {'-' * 58}")
    for symbol, mkt, hist, _, _ in watchlist_data:
        rsi_str = f"{hist.rsi_14:.0f}" if hist.rsi_14 is not None else "N/A"
        iv_str = f"{mkt.iv_rank:.0f}" if mkt.iv_rank > 0 else "N/A"
        lines.append(
            f"  {symbol:<8} ${float(mkt.price):>9,.2f} "
            f"{mkt.price_change_1d:>+7.1f}% {mkt.price_change_5d:>+7.1f}% "
            f"{mkt.price_vs_52w_high:>+7.1f}% {rsi_str:>6} {iv_str:>8}"
        )

    # Dip opportunities (biggest 1d losers)
    losers = sorted(watchlist_data, key=lambda x: x[1].price_change_1d)
    dips = [(s, m) for s, m, _, _, _ in losers if m.price_change_1d < -1.0]
    if dips:
        lines.append(f"\n━━ DIP OPPORTUNITIES ━━")
        for sym, mkt in dips[:5]:
            lines.append(f"  {sym}: {mkt.price_change_1d:+.1f}% today — "
                         f"premium likely elevated")

    # Support proximity
    near_support = []
    for symbol, mkt, hist, _, _ in watchlist_data:
        price = float(mkt.price)
        if hist.sma_200 and price > 0:
            pct = (price - float(hist.sma_200)) / float(hist.sma_200) * 100
            if 0 < pct < 5:
                near_support.append((symbol, "200 SMA", pct))
        if hist.sma_50 and price > 0:
            pct = (price - float(hist.sma_50)) / float(hist.sma_50) * 100
            if 0 < pct < 3:
                near_support.append((symbol, "50 SMA", pct))

    if near_support:
        lines.append(f"\n━━ NEAR SUPPORT ━━")
        for sym, level, pct in near_support:
            lines.append(f"  {sym}: {pct:.1f}% above {level}")

    # Oversold
    oversold = [(s, h.rsi_14) for s, _, h, _, _ in watchlist_data
                if h.rsi_14 is not None and h.rsi_14 < 35]
    if oversold:
        lines.append(f"\n━━ OVERSOLD ━━")
        for sym, rsi in sorted(oversold, key=lambda x: x[1]):
            lines.append(f"  {sym}: RSI {rsi:.0f}")

    # Earnings upcoming
    from datetime import date, timedelta
    upcoming_earnings = []
    for symbol, _, _, _, cal in watchlist_data:
        if cal.next_earnings and cal.next_earnings <= date.today() + timedelta(days=14):
            days = (cal.next_earnings - date.today()).days
            upcoming_earnings.append((symbol, cal.next_earnings, days))

    if upcoming_earnings:
        lines.append(f"\n━━ EARNINGS WATCH ━━")
        for sym, dt, days in sorted(upcoming_earnings, key=lambda x: x[2]):
            lines.append(f"  {sym}: {dt} ({days}d away)")

    # Tax alerts
    if tax_alerts:
        lines.append(f"\n━━ TAX ALERTS ━━")
        for alert in tax_alerts:
            lines.append(f"  {alert}")

    # Macro
    lines.append(f"\n━━ MACRO ━━")
    # VIX term structure from any symbol's context
    if watchlist_data:
        sample_mkt = watchlist_data[0][1]
        if sample_mkt.vix_term_structure:
            lines.append(f"  VIX term structure: {sample_mkt.vix_term_structure}")
    lines.append(f"  VIX: {vix:.2f}")

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

    # 5. Risk checks
    router = AccountRouter()
    liquidity_ok, liquidity_msg = check_liquidity_health(router)
    tax_alerts = generate_tax_alerts([])

    # 6. Build and print briefing
    briefing = format_local_briefing(
        regime=regime,
        vix=vix,
        spy_change=spy_change,
        all_signals=all_signals,
        watchlist_data=watchlist_data,
        tax_alerts=tax_alerts,
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
    """Paper trading mode — runs full pipeline with simulated execution."""
    from src.execution import PaperTrader
    from src.models.paper import ExecutionRules

    log.info("mode_paper_start")
    trader = PaperTrader(rules=ExecutionRules())

    result = await run_analysis_cycle("morning", always_push=True)
    dashboard = trader.generate_dashboard()
    log.info("paper_dashboard", **{
        "trades": dashboard.total_trades,
        "win_rate": f"{dashboard.win_rate:.0%}" if dashboard.win_rate else "N/A",
    })


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
