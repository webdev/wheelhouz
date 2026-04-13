"""Brokerage account models and routing logic."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class BrokerageAccount:
    """A single brokerage account with its constraints."""
    account_id: str
    account_type: str       # "taxable", "roth_ira", "traditional_ira", "401k_rollover"

    # Balances
    total_value: Decimal = Decimal("0")
    cash_available: Decimal = Decimal("0")
    buying_power: Decimal = Decimal("0")

    # Options permissions (E*Trade levels 1-4)
    options_level: int = 0

    # Restrictions
    margin_enabled: bool = False
    can_short_stock: bool = False

    # Liquidity
    withdrawal_restricted: bool = False
    early_withdrawal_penalty: float = 0.0

    # Contribution limits
    annual_contribution_limit: Decimal = Decimal("0")
    contributions_this_year: Decimal = Decimal("0")
    remaining_contribution_room: Decimal = Decimal("0")

    # Roth-specific
    roth_contribution_basis: Decimal = Decimal("0")
    roth_earnings: Decimal = Decimal("0")

    @property
    def liquid_value(self) -> Decimal:
        """How much can be withdrawn without penalty."""
        if self.account_type == "taxable":
            return self.total_value
        elif self.account_type == "roth_ira":
            return self.roth_contribution_basis
        else:
            return Decimal("0")

    @property
    def allowed_strategies(self) -> list[str]:
        """Strategies this account can execute based on options level."""
        strategies: list[str] = []
        if self.options_level >= 1:
            strategies.extend(["covered_call", "protective_put"])
        if self.options_level >= 2:
            strategies.extend(["monthly_put", "weekly_put", "dividend_capture"])
        if self.options_level >= 3:
            strategies.extend(["put_spread", "call_spread"])
        if self.options_level >= 4:
            strategies.extend(["strangle", "naked_put", "earnings_crush"])
        return strategies


@dataclass
class AccountRouter:
    """Routes trades to optimal account for tax efficiency and liquidity."""
    accounts: dict[str, BrokerageAccount] = field(default_factory=dict)

    # Liquidity requirements
    min_liquid_pct: float = 0.60
    min_liquid_dollars: Decimal = Decimal("100000")
    emergency_reserve_months: int = 6
    monthly_expenses: Decimal = Decimal("10000")

    @property
    def total_nlv(self) -> Decimal:
        """Total net liquidation value across all accounts."""
        return sum(
            (a.total_value for a in self.accounts.values()),
            Decimal("0"),
        )

    @property
    def total_liquid(self) -> Decimal:
        """Total liquid value across all accounts."""
        return sum(
            (a.liquid_value for a in self.accounts.values()),
            Decimal("0"),
        )

    @property
    def liquidity_ratio(self) -> float:
        """Fraction of total NLV that is liquid."""
        nlv = self.total_nlv
        if nlv == 0:
            return 0.0
        return float(self.total_liquid / nlv)
