"""Alpha signal detection — the 13 signals that drive trade selection.

Each detector takes market data and returns AlphaSignal | None.
Signals have strength (0-100) and expiry. The analysis pipeline
aggregates signals per symbol, then feeds them to sizing.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from src.config.loader import load_trading_params
from src.models.enums import SignalType
from src.models.market import EventCalendar, MarketContext, OptionsChain, PriceHistory
from src.models.signals import AlphaSignal


def _now() -> datetime:
    return datetime.utcnow()


def _params() -> dict[str, object]:
    return load_trading_params().get("signals", {})  # type: ignore[no-any-return]


# ── 1. Intraday Dip ─────────────────────────────────────────────


def detect_intraday_dip(
    symbol: str,
    mkt: MarketContext,
) -> AlphaSignal | None:
    """Stock down 2.5%+ intraday with no earnings catalyst."""
    params = _params()
    threshold = float(params.get("intraday_dip_threshold", -2.5))  # type: ignore[arg-type]

    if mkt.price_change_1d > threshold:
        return None

    strength = min(100.0, abs(mkt.price_change_1d) * 15)
    if mkt.iv_rank > 50:
        strength = min(100.0, strength * 1.3)

    return AlphaSignal(
        symbol=symbol,
        signal_type=SignalType.INTRADAY_DIP,
        strength=strength,
        direction="sell_put",
        reasoning=f"{symbol} down {mkt.price_change_1d:.1f}% today, "
                  f"IV rank {mkt.iv_rank:.0f}. Sell into fear.",
        expires=_now() + timedelta(hours=24),
    )


# ── 2. Multi-Day Pullback ───────────────────────────────────────


def detect_multi_day_pullback(
    symbol: str,
    hist: PriceHistory,
) -> AlphaSignal | None:
    """3+ consecutive red days, stock down 5%+ from recent high."""
    params = _params()
    min_days = int(params.get("multi_day_pullback_days", 3))  # type: ignore[call-overload]
    min_pct = float(params.get("multi_day_pullback_pct", 5.0))  # type: ignore[arg-type]

    red_days = hist.consecutive_red_days()
    drawdown = hist.drawdown_from_n_day_high(5)

    if red_days < min_days or drawdown < min_pct:
        return None

    strength = min(100.0, drawdown * 10 + red_days * 5)
    return AlphaSignal(
        symbol=symbol,
        signal_type=SignalType.MULTI_DAY_PULLBACK,
        strength=strength,
        direction="sell_put",
        reasoning=f"{symbol}: {red_days} red days, down {drawdown:.1f}% "
                  f"from 5-day high.",
        expires=_now() + timedelta(hours=48),
    )


# ── 3. IV Rank Spike ────────────────────────────────────────────


def detect_iv_rank_spike(
    symbol: str,
    mkt: MarketContext,
) -> AlphaSignal | None:
    """IV rank > 60 and jumped significantly in 5 days."""
    if mkt.iv_rank <= 60 or mkt.iv_rank_change_5d <= 20:
        return None

    return AlphaSignal(
        symbol=symbol,
        signal_type=SignalType.IV_RANK_SPIKE,
        strength=min(100.0, mkt.iv_rank),
        direction="sell_put",
        reasoning=f"{symbol} IV rank spiked to {mkt.iv_rank:.0f} "
                  f"(+{mkt.iv_rank_change_5d:.0f} in 5 days). Premium is rich.",
        expires=_now() + timedelta(hours=48),
    )


# ── 4. Support Bounce ───────────────────────────────────────────


def detect_support_bounce(
    symbol: str,
    mkt: MarketContext,
    hist: PriceHistory,
) -> AlphaSignal | None:
    """Price within 3% of a major support level (200 SMA, 50 SMA, 52w low)."""
    params = _params()
    proximity = float(params.get("support_proximity_pct", 3.0))  # type: ignore[arg-type]

    price = float(mkt.price)
    support_levels: dict[str, Decimal | None] = {
        "200 SMA": hist.sma_200,
        "50 SMA": hist.sma_50,
        "52w low": hist.low_52w,
    }

    for level_name, level_price in support_levels.items():
        if level_price is None or level_price == 0:
            continue
        lp = float(level_price)
        pct_above = (price - lp) / lp * 100
        if 0 < pct_above < proximity:
            return AlphaSignal(
                symbol=symbol,
                signal_type=SignalType.SUPPORT_BOUNCE,
                strength=70.0,
                direction="sell_put",
                reasoning=f"{symbol} at ${price:.2f}, within {pct_above:.1f}% of "
                          f"{level_name} (${lp:.2f}). Sell puts at/below support.",
                expires=_now() + timedelta(hours=48),
            )
    return None


# ── 5. Oversold RSI ─────────────────────────────────────────────


def detect_oversold_rsi(
    symbol: str,
    hist: PriceHistory,
) -> AlphaSignal | None:
    """RSI(14) below 30 — mean reversion candidate."""
    params = _params()
    threshold = float(params.get("oversold_rsi_threshold", 30))  # type: ignore[arg-type]

    if hist.rsi_14 is None or hist.rsi_14 >= threshold:
        return None

    strength = min(100.0, (threshold - hist.rsi_14) * 5 + 50)
    return AlphaSignal(
        symbol=symbol,
        signal_type=SignalType.OVERSOLD_RSI,
        strength=strength,
        direction="sell_put",
        reasoning=f"{symbol} RSI(14) at {hist.rsi_14:.1f} — oversold. "
                  f"Mean reversion likely.",
        expires=_now() + timedelta(hours=72),
    )


# ── 6. Macro Fear ───────────────────────────────────────────────


def detect_macro_fear(
    symbol: str,
    mkt: MarketContext,
) -> AlphaSignal | None:
    """VIX > 25 and rising — broad fear premium elevated."""
    params = _params()
    vix_threshold = float(params.get("vix_fear_threshold", 25))  # type: ignore[arg-type]
    vix_change_min = float(params.get("vix_change_threshold", 2.0))  # type: ignore[arg-type]

    if mkt.vix is None or mkt.vix_change_1d is None:
        return None
    if mkt.vix <= vix_threshold or mkt.vix_change_1d <= vix_change_min:
        return None

    return AlphaSignal(
        symbol=symbol,
        signal_type=SignalType.MACRO_FEAR_SPIKE,
        strength=min(100.0, (mkt.vix - 20) * 5),
        direction="sell_put",
        reasoning=f"VIX at {mkt.vix:.1f} (+{mkt.vix_change_1d:.1f} today). "
                  f"Fear premium elevated. Sell aggressively.",
        expires=_now() + timedelta(hours=24),
    )


# ── 7. Skew Blowout ─────────────────────────────────────────────


def detect_skew_blowout(
    symbol: str,
    chain: OptionsChain,
) -> AlphaSignal | None:
    """Put skew > 30% above 30-day historical mean."""
    if chain.atm_iv is None or chain.historical_skew_25d is None:
        return None

    otm_put_iv = chain.get_iv_at_delta(-0.25)
    if otm_put_iv is None:
        return None

    skew = (otm_put_iv - chain.atm_iv) / chain.atm_iv * 100
    params = _params()
    multiplier = float(params.get("skew_blowout_multiplier", 1.3))  # type: ignore[arg-type]

    if chain.historical_skew_25d == 0 or skew <= chain.historical_skew_25d * multiplier:
        return None

    return AlphaSignal(
        symbol=symbol,
        signal_type=SignalType.SKEW_BLOW_OUT,
        strength=65.0,
        direction="sell_put",
        reasoning=f"{symbol} put skew at {skew:.1f}% vs "
                  f"{chain.historical_skew_25d:.1f}% normal. OTM puts overpriced.",
        expires=_now() + timedelta(hours=48),
    )


# ── 8. Term Structure Inversion ──────────────────────────────────


def detect_term_inversion(
    symbol: str,
    chain: OptionsChain,
) -> AlphaSignal | None:
    """Front-month IV > back-month IV — market pricing near-term fear."""
    front_iv = chain.iv_by_expiry.get("front_month")
    back_iv = chain.iv_by_expiry.get("second_month")

    if front_iv is None or back_iv is None or back_iv == 0:
        return None

    params = _params()
    ratio_threshold = float(params.get("term_structure_inversion", 1.05))  # type: ignore[arg-type]

    if front_iv <= back_iv * ratio_threshold:
        return None

    return AlphaSignal(
        symbol=symbol,
        signal_type=SignalType.TERM_STRUCTURE_INVERSION,
        strength=60.0,
        direction="sell_put",
        reasoning=f"{symbol} term structure inverted: front IV {front_iv:.1f}% vs "
                  f"back {back_iv:.1f}%. Sell front month for inversion premium.",
        expires=_now() + timedelta(hours=24),
    )


# ── 9. Earnings Overreaction ─────────────────────────────────────


def detect_earnings_overreaction(
    symbol: str,
    mkt: MarketContext,
    hist: PriceHistory,
    cal: EventCalendar,
) -> AlphaSignal | None:
    """Post-earnings gap > 8%, RSI < 25 — likely oversold reaction."""
    if cal.next_earnings is None:
        return None

    # Only fire if earnings happened recently (within 3 days)
    days_since = (date.today() - cal.next_earnings).days
    if days_since < 0 or days_since > 3:
        return None

    if mkt.price_change_1d > -8.0:
        return None
    if hist.rsi_14 is None or hist.rsi_14 > 25:
        return None

    strength = min(100.0, abs(mkt.price_change_1d) * 8)
    return AlphaSignal(
        symbol=symbol,
        signal_type=SignalType.EARNINGS_OVERREACTION,
        strength=strength,
        direction="sell_put",
        reasoning=f"{symbol} post-earnings gap {mkt.price_change_1d:.1f}%, "
                  f"RSI {hist.rsi_14:.1f}. Likely overreaction.",
        expires=_now() + timedelta(hours=72),
    )


# ── 10. Sector Rotation ─────────────────────────────────────────


def detect_sector_rotation(
    symbol: str,
    mkt: MarketContext,
) -> AlphaSignal | None:
    """Symbol underperforming vs broad market by 2+ standard deviations.

    Simplified: if stock down >3% while VIX is flat/down, it's sector-specific.
    """
    if mkt.vix is None or mkt.vix_change_1d is None:
        return None

    # Stock down hard but VIX not spiking → sector-specific selling
    if mkt.price_change_5d > -5.0:
        return None
    if mkt.vix_change_1d > 1.0:
        return None  # broad fear, not sector rotation

    return AlphaSignal(
        symbol=symbol,
        signal_type=SignalType.SECTOR_ROTATION,
        strength=55.0,
        direction="sell_put",
        reasoning=f"{symbol} down {mkt.price_change_5d:.1f}% over 5 days "
                  f"while VIX flat. Sector rotation, not systemic.",
        expires=_now() + timedelta(hours=48),
    )


# ── 11. Volume Climax ───────────────────────────────────────────


def detect_volume_climax(
    symbol: str,
    mkt: MarketContext,
    hist: PriceHistory,
) -> AlphaSignal | None:
    """Volume > 3x 20-day average on a down day — capitulation selling."""
    if not hist.daily_volumes or len(hist.daily_volumes) < 20:
        return None
    if mkt.price_change_1d >= 0:
        return None  # only on down days

    avg_20d = sum(hist.daily_volumes[-20:]) / 20
    if avg_20d == 0:
        return None

    latest_vol = hist.daily_volumes[-1]
    vol_ratio = latest_vol / avg_20d

    if vol_ratio < 3.0:
        return None

    return AlphaSignal(
        symbol=symbol,
        signal_type=SignalType.VOLUME_CLIMAX,
        strength=min(100.0, vol_ratio * 15),
        direction="sell_put",
        reasoning=f"{symbol} volume {vol_ratio:.1f}x average on a down day. "
                  f"Capitulation selling — exhaustion likely.",
        expires=_now() + timedelta(hours=48),
    )


# ── 12. Gap Fill ─────────────────────────────────────────────────


def detect_gap_fill(
    symbol: str,
    hist: PriceHistory,
) -> AlphaSignal | None:
    """Price filling toward prior close after gap down — support zone."""
    if len(hist.daily_closes) < 3:
        return None

    prev_close = hist.daily_closes[-2]
    today_price = hist.current_price

    if prev_close == 0:
        return None

    # There was a gap down (open much lower than prev close)
    # and price is now recovering toward the prior close
    gap_pct = float((today_price - prev_close) / prev_close * 100)

    # Gap down of 2%+ that is now within 1% of filling
    if gap_pct < -2.0 and gap_pct > -4.0:
        return AlphaSignal(
            symbol=symbol,
            signal_type=SignalType.GAP_FILL,
            strength=50.0,
            direction="sell_put",
            reasoning=f"{symbol} gap down {gap_pct:.1f}%, filling toward "
                      f"prior close. Gap fill support zone.",
            expires=_now() + timedelta(hours=24),
        )
    return None


# ── 14. Overbought RSI (Call Signal) ────────────────────────────


def detect_overbought_rsi(
    symbol: str,
    hist: PriceHistory,
) -> AlphaSignal | None:
    """RSI(14) above 70 — stock extended, sell calls into strength."""
    params = _params()
    threshold = float(params.get("overbought_rsi_threshold", 70))  # type: ignore[arg-type]

    if hist.rsi_14 is None or hist.rsi_14 <= threshold:
        return None

    strength = min(100.0, (hist.rsi_14 - threshold) * 5 + 50)
    return AlphaSignal(
        symbol=symbol,
        signal_type=SignalType.OVERBOUGHT_RSI,
        strength=strength,
        direction="sell_call",
        reasoning=f"{symbol} RSI(14) at {hist.rsi_14:.1f} — overbought. "
                  f"Sell covered calls into strength.",
        expires=_now() + timedelta(hours=72),
    )


# ── 15. Resistance Test (Call Signal) ──────────────────────────


def detect_resistance_test(
    symbol: str,
    mkt: MarketContext,
    hist: PriceHistory,
) -> AlphaSignal | None:
    """Price within 3% of major resistance (52w high, SMA above price)."""
    params = _params()
    proximity = float(params.get("resistance_proximity_pct", 3.0))  # type: ignore[arg-type]

    price = float(mkt.price)
    resistance_levels: dict[str, Decimal | None] = {
        "52w high": hist.high_52w if hist.high_52w > 0 else None,
        "200 SMA": hist.sma_200 if hist.sma_200 and hist.sma_200 > hist.current_price else None,
        "50 SMA": hist.sma_50 if hist.sma_50 and hist.sma_50 > hist.current_price else None,
    }

    for level_name, level_price in resistance_levels.items():
        if level_price is None or level_price == 0:
            continue
        lp = float(level_price)
        pct_below = (lp - price) / lp * 100
        if 0 < pct_below < proximity:
            return AlphaSignal(
                symbol=symbol,
                signal_type=SignalType.RESISTANCE_TEST,
                strength=65.0,
                direction="sell_call",
                reasoning=f"{symbol} at ${price:.2f}, within {pct_below:.1f}% of "
                          f"{level_name} (${lp:.2f}). Sell calls at/above resistance.",
                expires=_now() + timedelta(hours=48),
            )
    return None


# ── 16. Multi-Day Rally (Call Signal) ──────────────────────────


def detect_multi_day_rally(
    symbol: str,
    hist: PriceHistory,
) -> AlphaSignal | None:
    """3+ consecutive green days, stock up 5%+ from recent low."""
    params = _params()
    min_days = int(params.get("multi_day_rally_days", 3))  # type: ignore[call-overload]
    min_pct = float(params.get("multi_day_rally_pct", 5.0))  # type: ignore[arg-type]

    green_days = hist.consecutive_green_days()
    rally = hist.rally_from_n_day_low(5)

    if green_days < min_days or rally < min_pct:
        return None

    strength = min(100.0, rally * 10 + green_days * 5)
    return AlphaSignal(
        symbol=symbol,
        signal_type=SignalType.MULTI_DAY_RALLY,
        strength=strength,
        direction="sell_call",
        reasoning=f"{symbol}: {green_days} green days, up {rally:.1f}% "
                  f"from 5-day low. Sell covered calls into the rally.",
        expires=_now() + timedelta(hours=48),
    )


# ── 17. Volume Climax Up (Call Signal) ─────────────────────────


def detect_volume_climax_up(
    symbol: str,
    mkt: MarketContext,
    hist: PriceHistory,
) -> AlphaSignal | None:
    """Volume > 3x 20-day average on an UP day — exhaustion buying."""
    if not hist.daily_volumes or len(hist.daily_volumes) < 20:
        return None
    if mkt.price_change_1d <= 0:
        return None  # only on up days

    avg_20d = sum(hist.daily_volumes[-20:]) / 20
    if avg_20d == 0:
        return None

    latest_vol = hist.daily_volumes[-1]
    vol_ratio = latest_vol / avg_20d

    if vol_ratio < 3.0:
        return None

    return AlphaSignal(
        symbol=symbol,
        signal_type=SignalType.VOLUME_CLIMAX_UP,
        strength=min(100.0, vol_ratio * 15),
        direction="sell_call",
        reasoning=f"{symbol} volume {vol_ratio:.1f}x average on an up day. "
                  f"Exhaustion buying — sell covered calls.",
        expires=_now() + timedelta(hours=48),
    )


# ── 13. Dark Pool (deprioritized) ────────────────────────────────


def detect_dark_pool(
    symbol: str,
    mkt: MarketContext,
) -> AlphaSignal | None:
    """Unusual dark pool activity. Deprioritized — requires external data source."""
    # Placeholder: requires FINRA ADF/dark pool data feed
    # For now, returns None. Will implement when data source is available.
    return None


# ── Aggregate ────────────────────────────────────────────────────


def detect_all_signals(
    symbol: str,
    mkt: MarketContext,
    hist: PriceHistory,
    chain: OptionsChain,
    cal: EventCalendar,
) -> list[AlphaSignal]:
    """Run all 13 signal detectors and return those that fired."""
    results: list[AlphaSignal | None] = [
        # Put signals (sell puts on dips/weakness)
        detect_intraday_dip(symbol, mkt),
        detect_multi_day_pullback(symbol, hist),
        detect_iv_rank_spike(symbol, mkt),
        detect_support_bounce(symbol, mkt, hist),
        detect_oversold_rsi(symbol, hist),
        detect_macro_fear(symbol, mkt),
        detect_skew_blowout(symbol, chain),
        detect_term_inversion(symbol, chain),
        detect_earnings_overreaction(symbol, mkt, hist, cal),
        detect_sector_rotation(symbol, mkt),
        detect_volume_climax(symbol, mkt, hist),
        detect_gap_fill(symbol, hist),
        detect_dark_pool(symbol, mkt),
        # Call signals (sell covered calls on strength)
        detect_overbought_rsi(symbol, hist),
        detect_resistance_test(symbol, mkt, hist),
        detect_multi_day_rally(symbol, hist),
        detect_volume_climax_up(symbol, mkt, hist),
    ]
    return [s for s in results if s is not None]
