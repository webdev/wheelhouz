# src/analysis/bench.py
"""Bench builder — screens shopping list names for the briefing."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import structlog

from src.models.shopping_list import BenchEntry, ShoppingListEntry

log = structlog.get_logger()


def _check_near_actionable(
    iv_rank: float,
    rsi: float,
    next_earnings: date | None,
    today: date | None = None,
) -> tuple[bool, str | None]:
    """Check if a bench name is near-actionable.

    Triggers: earnings <7d (blackout watch), RSI <35, IV rank >55.
    """
    if today is None:
        today = date.today()
    reasons: list[str] = []

    if next_earnings and 0 < (next_earnings - today).days <= 7:
        reasons.append(
            f"Earns {next_earnings.strftime('%b')} {next_earnings.day} "
            f"\u2014 IN BLACKOUT, watch for post-earnings entry"
        )
    if rsi < 35:
        reasons.append(f"RSI {rsi:.0f} \u2014 oversold pullback entry")
    if iv_rank > 55:
        reasons.append(f"IV rank {iv_rank:.0f} \u2014 premium rich")

    if reasons:
        return True, ". ".join(reasons)
    return False, None


def _upside_pct(
    entry: ShoppingListEntry, current_price: Decimal,
) -> float | None:
    """Calculate upside percentage from current price to 2026 target midpoint."""
    if entry.price_target_2026 and current_price > 0:
        midpoint = (entry.price_target_2026[0] + entry.price_target_2026[1]) / 2
        return float((midpoint - current_price) / current_price)
    return None


def _rank_and_filter(
    shopping_list: list[ShoppingListEntry],
    watchlist: set[str],
    scanner_symbols: set[str],
) -> list[ShoppingListEntry]:
    """Filter and rank shopping list entries for bench consideration.

    Excludes: watchlist, scanner picks, Sell-rated, no ticker.
    Returns top 30 by rating tier.
    """
    filtered: list[ShoppingListEntry] = []
    for entry in shopping_list:
        if entry.ticker in watchlist:
            continue
        if entry.ticker in scanner_symbols:
            continue
        if entry.rating_tier == 0:  # Sell
            continue
        if not entry.ticker:
            continue
        if "." in entry.ticker:
            continue  # non-US exchange (e.g. 0A5W.IL)
        filtered.append(entry)

    # Sort by rating tier descending
    filtered.sort(key=lambda e: e.rating_tier, reverse=True)
    return filtered[:30]


async def build_bench(
    shopping_list: list[ShoppingListEntry],
    watchlist: set[str],
    scanner_symbols: set[str],
) -> list[BenchEntry]:
    """Build the bench — top shopping list names with lightweight technicals.

    Fetches current price, RSI, IV rank proxy, and next earnings via yfinance.
    Returns top 15 ranked by composite score.
    """
    import asyncio
    import yfinance as yf

    candidates = _rank_and_filter(shopping_list, watchlist, scanner_symbols)
    if not candidates:
        return []

    tickers = [c.ticker for c in candidates]
    entry_map = {c.ticker: c for c in candidates}

    # Batch fetch lightweight data (yf.download is sync — run in thread)
    try:
        data = await asyncio.to_thread(
            yf.download,
            tickers, period="3mo", interval="1d", group_by="ticker",
            auto_adjust=True, progress=False, threads=True,
        )
    except Exception as e:
        log.warning("bench_yf_download_failed", error=str(e))
        return []

    # Fetch earnings dates concurrently for all candidates
    def _get_earnings(t: str) -> date | None:
        try:
            tk = yf.Ticker(t)
            cal = tk.calendar
            if cal is not None and not cal.empty:
                earnings_dates = cal.get("Earnings Date")
                if earnings_dates is not None and len(earnings_dates) > 0:
                    ed = earnings_dates[0]
                    return ed.date() if hasattr(ed, 'date') else None
        except Exception:
            pass
        return None

    earnings_tasks = [asyncio.to_thread(_get_earnings, t) for t in tickers]
    earnings_results = await asyncio.gather(*earnings_tasks, return_exceptions=True)
    earnings_by_ticker: dict[str, date | None] = {}
    for t, result in zip(tickers, earnings_results):
        earnings_by_ticker[t] = result if isinstance(result, date) else None

    bench_entries: list[tuple[float, BenchEntry]] = []

    for ticker in tickers:
        entry = entry_map[ticker]
        try:
            if len(tickers) == 1:
                hist = data
            else:
                hist = data[ticker] if ticker in data.columns.get_level_values(0) else None
            if hist is None or hist.empty:
                continue

            close = hist["Close"].dropna()
            if len(close) < 14:
                continue

            current_price = Decimal(str(round(float(close.iloc[-1]), 2)))

            # RSI(14)
            delta = close.diff()
            gain = delta.where(delta > 0, 0.0).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
            rs = gain.iloc[-1] / loss.iloc[-1] if loss.iloc[-1] != 0 else 100
            rsi = float(100 - (100 / (1 + rs)))

            # IV rank proxy — rolling HV percentile over available history.
            # For each rolling 20-day window, compute annualised HV, then report
            # where today's 20-day HV sits in [min, max] of that series (0-100).
            # Works correctly with ~40 bars (3 months of daily data).
            if len(close) >= 20:
                returns = close.pct_change().dropna()
                ann = 252 ** 0.5 * 100
                hv_series = returns.rolling(20).std().dropna() * ann
                if len(hv_series) >= 2:
                    hv_current = float(hv_series.iloc[-1])
                    hv_min = float(hv_series.min())
                    hv_max = float(hv_series.max())
                    iv_rank = (
                        min(100.0, max(0.0, (hv_current - hv_min) / (hv_max - hv_min) * 100))
                        if hv_max > hv_min else 50.0
                    )
                else:
                    iv_rank = 50.0
            else:
                iv_rank = 50.0

            next_earnings_date = earnings_by_ticker.get(ticker)
            upside = _upside_pct(entry, current_price)

            # Price target display string
            pt_display = None
            if entry.price_target_2026:
                low, high = entry.price_target_2026
                if low == high:
                    pt_display = f"{low:,.0f}"
                else:
                    pt_display = f"{low:,.0f}-{high:,.0f}"

            # Near-actionable check
            actionable, actionable_reason = _check_near_actionable(
                iv_rank=iv_rank, rsi=rsi, next_earnings=next_earnings_date,
            )

            # Composite score for ranking
            upside_norm = min(max(upside, 0.0), 1.0) if upside is not None else 0.0
            iv_norm = min(iv_rank / 100.0, 1.0)
            rsi_bonus = 2.0 if rsi < 30 else (1.0 if rsi < 40 else 0.0)
            composite = entry.rating_tier * 3 + upside_norm * 2 + iv_norm + rsi_bonus

            # Dynamic entry price — nearest support level
            entry_price = None
            entry_label = None
            if len(close) >= 20:
                cur = float(close.iloc[-1])
                sma_20 = float(close.rolling(20).mean().iloc[-1])
                ema_9 = float(close.ewm(span=9, adjust=False).mean().iloc[-1])
                sma_50 = float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None
                levels: list[tuple[float, str]] = []
                if ema_9 > 0 and ema_9 < cur:
                    levels.append((ema_9, "EMA 9"))
                if sma_20 > 0 and sma_20 < cur:
                    levels.append((sma_20, "SMA 20"))
                if sma_50 and sma_50 > 0 and sma_50 < cur:
                    levels.append((sma_50, "SMA 50"))
                levels.sort(key=lambda x: x[0], reverse=True)
                for lvl, lbl in levels:
                    if lvl < cur * 0.98:
                        entry_price = round(lvl, 2)
                        entry_label = lbl
                        break
                if entry_price is None and levels:
                    entry_price = round(levels[0][0], 2)
                    entry_label = levels[0][1]

            target_low = float(entry.price_target_2026[0]) if entry.price_target_2026 else None
            target_high = float(entry.price_target_2026[1]) if entry.price_target_2026 else None

            bench_entry = BenchEntry(
                ticker=ticker,
                name=entry.name,
                rating=entry.rating,
                current_price=current_price,
                price_target=pt_display,
                upside_pct=upside,
                iv_rank=iv_rank,
                rsi=rsi,
                next_earnings=next_earnings_date,
                near_actionable=actionable,
                actionable_reason=actionable_reason,
                entry_price=entry_price,
                entry_label=entry_label,
                target_low=target_low,
                target_high=target_high,
            )
            bench_entries.append((composite, bench_entry))

        except Exception as e:
            log.debug("bench_entry_failed", ticker=ticker, error=str(e))
            continue

    # Sort by composite score descending, return top 15
    bench_entries.sort(key=lambda x: x[0], reverse=True)
    result = [be for _, be in bench_entries[:15]]
    log.info("bench_built", entries=len(result))
    return result
