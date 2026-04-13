"""Tax engine models: tax tracking, wash sales, trade tax impact."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal


@dataclass
class WashSaleTracker:
    """Tracks wash sale windows for closed-at-a-loss positions."""
    active_windows: dict[str, date] = field(default_factory=dict)

    def record_loss(self, symbol: str, loss_date: date, loss_amount: Decimal) -> None:
        """Record a loss close. Opens a 30-calendar-day wash sale window."""
        self.active_windows[symbol] = loss_date + timedelta(days=30)

    def check_before_trade(self, symbol: str) -> tuple[bool, str | None]:
        """Check if a new position would trigger a wash sale."""
        window_end = self.active_windows.get(symbol)
        if window_end and date.today() <= window_end:
            days_remaining = (window_end - date.today()).days
            loss_date = window_end - timedelta(days=30)
            return (
                False,
                f"WASH SALE: {symbol} closed at a loss on {loss_date}. "
                f"Wait {days_remaining} more days.",
            )
        return (True, None)

    def get_blocked_tickers(self) -> list[str]:
        """Return all tickers currently in a wash sale window."""
        today = date.today()
        return [sym for sym, end in self.active_windows.items() if end >= today]


@dataclass
class TaxContext:
    """Tax information for a single position."""
    symbol: str
    cost_basis_per_share: Decimal
    current_price: Decimal
    unrealized_gain: Decimal
    unrealized_gain_pct: float
    purchase_date: date
    holding_period_days: int
    is_ltcg: bool

    # Tax impact of selling now
    estimated_tax_if_sold: Decimal = Decimal("0")
    estimated_tax_if_waited_for_ltcg: Decimal = Decimal("0")
    tax_savings_by_waiting: Decimal = Decimal("0")
    days_until_ltcg: int | None = None


@dataclass
class TaxEngine:
    """Real-time tax tracking across the entire portfolio."""
    # Rates
    short_term_rate: float = 0.37
    long_term_rate: float = 0.20
    niit_rate: float = 0.038
    state_rate: float = 0.00

    @property
    def stcg_effective(self) -> float:
        """Effective short-term capital gains rate."""
        return self.short_term_rate + self.niit_rate + self.state_rate

    @property
    def ltcg_effective(self) -> float:
        """Effective long-term capital gains rate."""
        return self.long_term_rate + self.niit_rate + self.state_rate

    # Running tallies (reset annually)
    realized_stcg_ytd: Decimal = Decimal("0")
    realized_ltcg_ytd: Decimal = Decimal("0")
    realized_losses_ytd: Decimal = Decimal("0")
    option_premium_income_ytd: Decimal = Decimal("0")

    # Tax-loss harvesting
    harvested_losses_ytd: Decimal = Decimal("0")
    remaining_loss_carryforward: Decimal = Decimal("0")

    # Wash sale tracking
    wash_sale_tracker: WashSaleTracker = field(default_factory=WashSaleTracker)


@dataclass
class TradeTaxImpact:
    """Estimated tax impact for a single trade."""
    trade_description: str
    gross_pnl: Decimal

    # Classification
    is_short_term: bool
    holding_period_days: int

    # Tax calculation
    tax_rate: float
    estimated_tax: Decimal
    net_after_tax: Decimal

    # Context
    wash_sale_risk: bool = False
    wash_sale_ticker: str | None = None
    wash_sale_warning: str | None = None

    # Optimization
    can_offset_with_losses: bool = False
    loss_offset_amount: Decimal = Decimal("0")
    net_tax_after_offset: Decimal = Decimal("0")
