"""Smart strike selection — pick strikes at technically meaningful levels.

A pro doesn't just pick the nearest delta — they pick strikes at levels
where institutions add (200 SMA, VWAP, 52w low, swing lows).
"""

from __future__ import annotations

from decimal import Decimal

from src.config.loader import load_trading_params
from src.models.analysis import SmartStrike
from src.models.market import OptionsChain, PriceHistory


def find_smart_strikes(
    symbol: str,
    chain: OptionsChain,
    hist: PriceHistory,
    direction: str,
    target_dte: int = 30,
) -> list[SmartStrike]:
    """Find the best strikes at technical levels.

    Returns a ranked list of SmartStrike, best first.
    """
    params = load_trading_params().get("wheel", {})
    if direction == "sell_put":
        min_delta = abs(float(params.get("put_max_delta", -0.15)))
        max_delta = abs(float(params.get("put_min_delta", -0.40)))
        target_delta = abs(float(params.get("put_target_delta", -0.30)))
    else:
        min_delta = float(params.get("call_min_delta", 0.15))
        max_delta = float(params.get("call_max_delta", 0.40))
        target_delta = float(params.get("call_target_delta", 0.30))

    min_yield = float(params.get("min_annualized_yield", 0.15))

    # Collect candidate support/resistance levels
    levels = _find_technical_levels(hist, direction)

    # Score each level as a potential strike
    candidates: list[SmartStrike] = []
    price = float(hist.current_price)

    for level_price, reason in levels:
        lp = float(level_price)
        if lp <= 0 or price <= 0:
            continue

        # Estimate delta from distance (simplified — real delta comes from chain)
        if direction == "sell_put":
            distance_pct = (price - lp) / price
            # Rough delta approximation: further OTM → lower delta
            est_delta = max(0.05, 0.50 - distance_pct * 3.0)
        else:
            distance_pct = (lp - price) / price
            est_delta = max(0.05, 0.50 - distance_pct * 3.0)

        if est_delta < min_delta or est_delta > max_delta:
            continue

        # Estimate premium (rough: ATM IV * sqrt(DTE/365) * strike * delta-proxy)
        atm_iv = chain.atm_iv or 0.30
        time_factor = (target_dte / 365.0) ** 0.5
        est_premium = Decimal(str(round(lp * atm_iv * time_factor * est_delta * 0.5, 2)))

        if est_premium <= 0:
            continue

        capital = Decimal(str(lp)) * 100
        yield_on_cap = float(est_premium * 100 / capital) if capital > 0 else 0.0
        ann_yield = yield_on_cap * (365.0 / target_dte) if target_dte > 0 else 0.0

        # Score: closer to target delta + technical reason + yield
        delta_score = 1.0 - abs(est_delta - target_delta) / target_delta if target_delta else 0
        yield_score = min(1.0, ann_yield / 0.30)  # normalize to 30% ann
        tech_score = 1.0  # all levels are technically significant
        score = delta_score * 30 + yield_score * 40 + tech_score * 30

        candidates.append(SmartStrike(
            strike=Decimal(str(round(lp, 2))),
            delta=-est_delta if direction == "sell_put" else est_delta,
            premium=est_premium,
            yield_on_capital=round(yield_on_cap, 4),
            annualized_yield=round(ann_yield, 4),
            technical_reason=reason,
            strike_score=round(score, 1),
        ))

    # Sort by score descending
    candidates.sort(key=lambda s: s.strike_score, reverse=True)
    return candidates


def _find_technical_levels(
    hist: PriceHistory,
    direction: str,
) -> list[tuple[Decimal, str]]:
    """Identify technically meaningful price levels for strike selection."""
    levels: list[tuple[Decimal, str]] = []
    price = hist.current_price

    if direction == "sell_put":
        # For puts, we want support levels BELOW current price
        if hist.sma_200 is not None and hist.sma_200 < price:
            levels.append((hist.sma_200, "200 SMA support"))
        if hist.sma_50 is not None and hist.sma_50 < price:
            levels.append((hist.sma_50, "50 SMA support"))
        if hist.low_52w < price:
            levels.append((hist.low_52w, "52-week low"))
        if hist.recent_swing_low is not None and hist.recent_swing_low < price:
            levels.append((hist.recent_swing_low, "20-day swing low"))
        if hist.anchored_vwap_90d is not None and hist.anchored_vwap_90d < price:
            levels.append((hist.anchored_vwap_90d, "90-day VWAP"))

        # Round number levels (psychological support)
        price_f = float(price)
        for pct in (0.05, 0.10, 0.15):
            round_level = _round_down(price_f * (1 - pct))
            levels.append((Decimal(str(round_level)), f"{pct:.0%} below current"))

    else:
        # For calls, we want resistance levels ABOVE current price
        if hist.sma_200 is not None and hist.sma_200 > price:
            levels.append((hist.sma_200, "200 SMA resistance"))
        if hist.sma_50 is not None and hist.sma_50 > price:
            levels.append((hist.sma_50, "50 SMA resistance"))
        if hist.high_52w > price:
            levels.append((hist.high_52w, "52-week high"))
        if hist.recent_swing_high is not None and hist.recent_swing_high > price:
            levels.append((hist.recent_swing_high, "20-day swing high"))

        price_f = float(price)
        for pct in (0.05, 0.10, 0.15):
            round_level = _round_up(price_f * (1 + pct))
            levels.append((Decimal(str(round_level)), f"{pct:.0%} above current"))

    return levels


def _round_down(price: float) -> float:
    """Round down to nearest 'clean' strike (5s for <100, 10s for >100)."""
    if price < 50:
        return float(int(price))
    elif price < 200:
        return float(int(price / 5) * 5)
    else:
        return float(int(price / 10) * 10)


def _round_up(price: float) -> float:
    """Round up to nearest 'clean' strike."""
    if price < 50:
        return float(int(price) + 1)
    elif price < 200:
        return float((int(price / 5) + 1) * 5)
    else:
        return float((int(price / 10) + 1) * 10)
