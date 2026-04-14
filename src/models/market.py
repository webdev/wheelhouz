"""Market data and event calendar models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass
class MarketContext:
    """IV and price context for a single symbol."""
    symbol: str
    iv_rank: float
    iv_percentile: float
    iv_rank_change_5d: float
    iv_30d: float
    hv_30d: float
    iv_hv_spread: float

    # Price context
    price: Decimal
    price_change_1d: float
    price_change_5d: float
    price_vs_52w_high: float
    price_vs_200sma: float

    # Volume/flow
    put_call_ratio: float
    option_volume_vs_avg: float

    # Macro (shared across all symbols)
    vix: float | None = None
    vix_change_1d: float | None = None
    vix_term_structure: str | None = None  # "contango" or "backwardation"


@dataclass
class PriceHistory:
    """Technical context for strike selection and signal detection."""
    symbol: str
    current_price: Decimal

    # Moving averages
    sma_200: Decimal | None = None
    sma_50: Decimal | None = None
    sma_20: Decimal | None = None
    ema_9: Decimal | None = None

    # Key levels
    high_52w: Decimal = Decimal("0")
    low_52w: Decimal = Decimal("0")
    recent_swing_high: Decimal | None = None
    recent_swing_low: Decimal | None = None
    anchored_vwap_90d: Decimal | None = None

    # Momentum / mean reversion
    rsi_14: float | None = None

    # History arrays
    daily_closes: list[Decimal] = field(default_factory=list)
    daily_volumes: list[float] = field(default_factory=list)

    def last_n_closes(self, n: int) -> list[Decimal]:
        """Return the last n closing prices."""
        return self.daily_closes[-n:]

    def consecutive_red_days(self) -> int:
        """Count consecutive down days from the most recent close."""
        count = 0
        closes = self.daily_closes
        for i in range(len(closes) - 1, 0, -1):
            if closes[i] < closes[i - 1]:
                count += 1
            else:
                break
        return count

    def consecutive_green_days(self) -> int:
        """Count consecutive up days from the most recent close."""
        count = 0
        closes = self.daily_closes
        for i in range(len(closes) - 1, 0, -1):
            if closes[i] > closes[i - 1]:
                count += 1
            else:
                break
        return count

    def drawdown_from_n_day_high(self, n: int) -> float:
        """Percent drawdown from the highest close in the last n days."""
        recent = self.daily_closes[-n:]
        if not recent:
            return 0.0
        peak = max(recent)
        if peak == 0:
            return 0.0
        return float((peak - self.current_price) / peak * 100)

    def rally_from_n_day_low(self, n: int) -> float:
        """Percent rally from the lowest close in the last n days."""
        recent = self.daily_closes[-n:]
        if not recent:
            return 0.0
        trough = min(recent)
        if trough == 0:
            return 0.0
        return float((self.current_price - trough) / trough * 100)


@dataclass
class EventCalendar:
    """Earnings, dividends, and macro events for a symbol."""
    symbol: str
    next_earnings: date | None = None
    earnings_confirmed: bool = False
    next_ex_dividend: date | None = None
    dividend_amount: Decimal | None = None

    # Macro events (shared across all positions)
    fed_meeting: date | None = None
    fed_speakers_today: list[str] = field(default_factory=list)
    cpi_ppi_date: date | None = None
    major_macro_event: str | None = None


@dataclass
class OptionContract:
    """A single option contract from a real chain."""
    strike: Decimal
    expiration: date
    option_type: str  # "put" or "call"
    bid: Decimal
    ask: Decimal
    mid: Decimal
    volume: int
    open_interest: int
    implied_vol: float
    delta: float


@dataclass
class OptionsChain:
    """Options chain data for a symbol."""
    symbol: str
    puts: list[OptionContract] = field(default_factory=list)
    calls: list[OptionContract] = field(default_factory=list)
    atm_iv: float | None = None
    historical_skew_25d: float | None = None
    iv_by_expiry: dict[str, float] = field(default_factory=dict)
    expirations: list[date] = field(default_factory=list)

    def get_iv_at_delta(self, delta: float) -> float | None:
        """Look up IV from real chain at nearest delta."""
        contracts = self.puts if delta < 0 else self.calls
        if not contracts:
            return None
        nearest = min(contracts, key=lambda c: abs(c.delta - delta))
        return nearest.implied_vol if abs(nearest.delta - delta) < 0.10 else None

    def get_expiry_near_dte(self, target_dte: int) -> date | None:
        """Find the expiration closest to target DTE."""
        if not self.expirations:
            return None
        today = date.today()
        return min(
            self.expirations,
            key=lambda d: abs((d - today).days - target_dte),
        )
