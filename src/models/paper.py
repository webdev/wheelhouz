"""Paper trading models: positions, snapshots, dashboard metrics."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal


@dataclass
class PaperPosition:
    """A simulated trading position tracked during paper trading."""
    symbol: str
    trade_type: str             # "sell_put", "sell_call"
    strike: Decimal
    expiration: date
    entry_price: Decimal        # premium received per contract
    entry_time: datetime
    contracts: int
    conviction: str             # "high", "medium", "low"
    signals: list[str] = field(default_factory=list)
    strategy: str = ""
    capital_at_risk: Decimal = Decimal("0")
    max_profit: Decimal = Decimal("0")

    # Updated in real-time
    current_price: Decimal = Decimal("0")
    current_underlying: Decimal = Decimal("0")
    current_pnl: Decimal = Decimal("0")
    profit_pct: float = 0.0

    # Set at close
    exit_price: Decimal | None = None
    exit_time: datetime | None = None
    exit_reason: str | None = None
    final_pnl: Decimal | None = None

    @property
    def days_to_expiry(self) -> int:
        return (self.expiration - date.today()).days

    @property
    def in_the_money(self) -> bool:
        """For short puts: ITM when underlying < strike."""
        if self.trade_type == "sell_put":
            return self.current_underlying < self.strike
        # For short calls: ITM when underlying > strike
        return self.current_underlying > self.strike


@dataclass
class PaperSnapshot:
    """Daily snapshot of paper trading performance."""
    date: date
    capital: Decimal
    buying_power: Decimal
    open_positions: int
    daily_pnl: Decimal
    cumulative_pnl: Decimal
    max_drawdown: float
    win_rate: float
    trades_to_date: int


@dataclass
class PaperDashboard:
    """Aggregated paper trading metrics for the go-live decision."""
    total_trades: int = 0
    winners: int = 0
    losers: int = 0
    win_rate: float = 0.0
    total_pnl: Decimal = Decimal("0")
    avg_winner: Decimal = Decimal("0")
    avg_loser: Decimal = Decimal("0")
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    annualized_return: float = 0.0

    # By conviction
    high_trades: int = 0
    high_win_rate: float = 0.0
    medium_trades: int = 0
    medium_win_rate: float = 0.0
    low_trades: int = 0
    low_win_rate: float = 0.0

    # Go-live checklist
    has_60_trades: bool = False
    has_55_win_rate: bool = False
    has_max_dd_under_12: bool = False
    has_high_wr_65: bool = False
    has_loss_stops_3: bool = False
    checks_passed: int = 0
    checks_total: int = 5
    ready_for_live: bool = False


@dataclass
class ExecutionRules:
    """Trading execution constraints and preferences."""
    preferred_entry_window: str = "10:00-10:30"
    avoid_first_15_min: bool = True     # never trade 9:30-9:45
    avoid_last_15_min: bool = True      # never trade 3:45-4:00
    avoid_fomc_day_before_2pm: bool = True

    default_order_type: str = "LIMIT"   # NEVER market orders on options
    limit_price_strategy: str = "mid_minus_penny"
    max_spread_pct: float = 0.05        # skip if bid-ask > 5% of mid
    use_combo_orders: bool = True       # for rolls and strangles

    # Slippage and commissions (paper trading realism)
    slippage_per_contract: Decimal = Decimal("0.03")
    commission_per_contract: Decimal = Decimal("0.65")
    fill_probability: float = 0.95
