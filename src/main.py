"""Wheel Copilot orchestrator — wires all modules together.

Run modes:
  python src/main.py                    # Full daemon (5x daily + monitor)
  python src/main.py --mode briefing    # Single morning briefing
  python src/main.py --mode paper       # Paper trading (Alpaca)
  python src/main.py --mode backtest    # Run backtests
  python src/main.py --mode onboard     # First-time onboarding
  python src/main.py --mode weekend-review  # Saturday learning loop
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, time
from decimal import Decimal
from typing import Any

import structlog

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
# Core pipeline
# ---------------------------------------------------------------------------

async def run_analysis_cycle(
    cycle_name: str,
    always_push: bool = False,
) -> dict[str, Any]:
    """Run a single analysis cycle.

    This is the canonical pipeline: data → signals → opportunities →
    risk → regime → briefing → delivery.
    """
    log.info("analysis_cycle_start", cycle=cycle_name)

    # 1. Data collection (would be async broker API calls)
    portfolio = await _refresh_portfolio()
    market_data = await _refresh_market_data()

    # 2. Signal detection (requires per-symbol data; stub returns empty)
    from src.models.signals import AlphaSignal
    signals: list[AlphaSignal] = []
    # In production: for each watchlist symbol, call
    #   detect_all_signals(symbol, mkt, hist, chain, cal)

    # 3. Regime classification
    from src.monitor.regime import classify_regime
    vix = market_data.get("vix", 20.0)
    spy_change = market_data.get("spy_change_pct", 0.0)
    regime = classify_regime(vix, spy_change)

    # 4. Risk checks
    from src.risk import check_liquidity_health
    from src.models.account import AccountRouter
    router = AccountRouter()
    liquidity_ok, liquidity_msg = check_liquidity_health(router)

    # 5. Tax alerts
    from src.risk import generate_tax_alerts
    tax_alerts = generate_tax_alerts([])

    result = {
        "cycle": cycle_name,
        "timestamp": datetime.utcnow().isoformat(),
        "regime": regime.regime,
        "signals_fired": len(signals),
        "liquidity_healthy": liquidity_ok,
        "tax_alerts": len(tax_alerts),
    }

    # 6. Delivery
    should_push = always_push or len(signals) > 0 or not liquidity_ok
    if should_push:
        log.info("pushing_briefing", cycle=cycle_name, reason="alert_conditions_met")

    log.info("analysis_cycle_complete", **result)
    return result


async def run_morning_briefing() -> dict[str, Any]:
    """Run the full morning briefing (8:00 AM cycle)."""
    return await run_analysis_cycle("morning", always_push=True)


async def run_sentinel_check() -> dict[str, Any]:
    """Run pre-market sentinel check."""
    from src.monitor.sentinel import check_premarket, format_sentinel_alert

    log.info("sentinel_check_start")
    # Would fetch real futures data
    alert = check_premarket(
        spy_futures_pct=0.0,
        nasdaq_futures_pct=0.0,
        vix_futures_change=0.0,
    )

    if alert.triggered:
        msg = format_sentinel_alert(alert)
        log.warning("sentinel_triggered", alert=msg)

    return {
        "triggered": alert.triggered,
        "severity": alert.severity,
    }


# ---------------------------------------------------------------------------
# Run modes
# ---------------------------------------------------------------------------

async def mode_briefing() -> None:
    """Single morning briefing run."""
    log.info("mode_briefing")
    result = await run_morning_briefing()
    log.info("briefing_complete", **result)


async def mode_paper() -> None:
    """Paper trading mode — runs full pipeline with simulated execution."""
    from src.execution import PaperTrader
    from src.models.paper import ExecutionRules

    log.info("mode_paper_start")
    trader = PaperTrader(rules=ExecutionRules())

    # Run the same analysis pipeline, but intercept execution
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

    # Would load real historical data
    results = []
    for signal in ["multi_day_pullback", "iv_rank_spike", "oversold_rsi"]:
        result = run_walk_forward(signal, {}, config)
        results.append(result)

    summary = format_backtest_summary(results)
    log.info("backtest_complete", signals_tested=len(results))
    print(summary)


async def mode_onboard() -> None:
    """First-time onboarding flow."""
    from src.delivery import auto_classify_portfolio, format_onboarding_summary

    log.info("mode_onboard_start")
    # Would connect to E*Trade and discover accounts
    log.info("onboard_complete", message="Interactive onboarding via Telegram")


async def mode_weekend_review() -> None:
    """Saturday learning loop + weekly review."""
    from src.learning import format_learning_report, run_weekly_review

    log.info("mode_weekend_review_start")
    # Would load closed trades from DB
    report = run_weekly_review([], {})
    output = format_learning_report(report)
    log.info("weekend_review_complete")
    print(output)


async def mode_daemon() -> None:
    """Full daemon mode — 5x daily analysis + continuous monitor + sentinel."""
    log.info("daemon_start", cycles=len(ANALYSIS_CYCLES))

    # Run morning briefing immediately
    await run_morning_briefing()

    # In production: schedule remaining cycles via APScheduler or similar
    log.info("daemon_running", message="Continuous monitor would start here")


# ---------------------------------------------------------------------------
# Stub data fetchers (replaced by real broker integration)
# ---------------------------------------------------------------------------

async def _refresh_portfolio() -> dict[str, Any]:
    """Fetch current portfolio state from broker."""
    return {
        "net_liquidation": Decimal("1000000"),
        "positions": [],
        "cash": Decimal("200000"),
    }


async def _refresh_market_data() -> dict[str, Any]:
    """Fetch current market data."""
    return {
        "vix": 20.0,
        "spy_change_pct": 0.0,
        "watchlist": [],
    }


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
