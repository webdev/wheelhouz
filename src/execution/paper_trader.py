"""Paper trading engine — simulated execution without real orders.

Uses live market data so the simulation reflects real conditions.
The ONLY difference from production: no broker order is placed.
Everything else (alerts, P&L, rules) is identical.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from src.models.analysis import SizedOpportunity
from src.models.execution import LivePriceGate
from src.models.paper import (
    ExecutionRules,
    PaperDashboard,
    PaperPosition,
    PaperSnapshot,
)


class PaperTrader:
    """Simulates the full trading system without placing real orders."""

    def __init__(
        self,
        initial_capital: Decimal = Decimal("100000"),
        rules: ExecutionRules | None = None,
    ) -> None:
        self.initial_capital = initial_capital
        self.current_capital = initial_capital
        self.buying_power = initial_capital

        self.open_positions: list[PaperPosition] = []
        self.closed_positions: list[PaperPosition] = []
        self.daily_snapshots: list[PaperSnapshot] = []

        self.rules = rules or ExecutionRules()

    # ── Opening trades ──────────────────────────────────────────

    def open_trade(
        self,
        gate: LivePriceGate,
        sized: SizedOpportunity,
        fill_price: Decimal | None = None,
    ) -> PaperPosition:
        """Simulate opening a new position.

        If fill_price is not provided, uses analysis premium minus slippage.
        """
        price = fill_price or (gate.analysis_premium - self.rules.slippage_per_contract)
        commission = self.rules.commission_per_contract * sized.contracts

        position = PaperPosition(
            symbol=gate.symbol,
            trade_type=gate.trade_type,
            strike=gate.strike,
            expiration=gate.expiration,
            entry_price=price,
            entry_time=datetime.utcnow(),
            contracts=sized.contracts,
            conviction=sized.conviction,
            signals=[s.signal_type.value for s in sized.signals],
            capital_at_risk=sized.capital_deployed,
            max_profit=price * sized.contracts * 100 - commission,
            current_price=price,
        )

        self.open_positions.append(position)
        self.buying_power -= sized.capital_deployed
        return position

    # ── Updating positions ──────────────────────────────────────

    def update_position(
        self,
        pos: PaperPosition,
        current_option_price: Decimal,
        current_underlying: Decimal,
    ) -> str | None:
        """Update a position's P&L and check exit rules.

        Returns exit reason if the position should be closed, else None.
        """
        pos.current_price = current_option_price
        pos.current_underlying = current_underlying
        pos.current_pnl = (
            (pos.entry_price - current_option_price) * pos.contracts * 100
        )
        if pos.max_profit > 0:
            pos.profit_pct = float(pos.current_pnl / pos.max_profit)

        # Profit target: close at 50%+ profit with >14 DTE
        if pos.profit_pct >= 0.50 and pos.days_to_expiry > 14:
            return "profit_target_50pct"

        # Loss stop: 2x for monthlies, 1.5x for weeklies
        if pos.entry_price > 0:
            loss_multiple = float(current_option_price / pos.entry_price)
            max_mult = 1.5 if pos.days_to_expiry <= 10 else 2.0
            if loss_multiple >= max_mult:
                return f"loss_stop_{max_mult}x"

        # Expiration
        if pos.days_to_expiry <= 0:
            return "assigned" if pos.in_the_money else "expired_worthless"

        return None

    # ── Closing positions ───────────────────────────────────────

    def close_position(self, pos: PaperPosition, reason: str) -> None:
        """Close a paper position and update capital."""
        commission = self.rules.commission_per_contract * pos.contracts
        pos.exit_price = pos.current_price
        pos.exit_time = datetime.utcnow()
        pos.exit_reason = reason
        pos.final_pnl = pos.current_pnl - commission

        if pos in self.open_positions:
            self.open_positions.remove(pos)
        self.closed_positions.append(pos)
        self.buying_power += pos.capital_at_risk
        self.current_capital += pos.final_pnl

    # ── Snapshots ───────────────────────────────────────────────

    def take_snapshot(self) -> PaperSnapshot:
        """Record daily state for performance tracking."""
        closed = self.closed_positions
        cumulative = sum(
            (p.final_pnl for p in closed if p.final_pnl is not None),
            Decimal("0"),
        )
        winners = [p for p in closed if p.final_pnl is not None and p.final_pnl > 0]
        win_rate = len(winners) / len(closed) if closed else 0.0

        snap = PaperSnapshot(
            date=date.today(),
            capital=self.current_capital,
            buying_power=self.buying_power,
            open_positions=len(self.open_positions),
            daily_pnl=Decimal("0"),  # computed from delta vs previous
            cumulative_pnl=cumulative,
            max_drawdown=self._max_drawdown(),
            win_rate=win_rate,
            trades_to_date=len(closed),
        )

        # Daily P&L = delta from previous snapshot
        if self.daily_snapshots:
            snap.daily_pnl = cumulative - self.daily_snapshots[-1].cumulative_pnl

        self.daily_snapshots.append(snap)
        return snap

    # ── Dashboard / go-live ─────────────────────────────────────

    def generate_dashboard(self) -> PaperDashboard:
        """Generate the paper trading dashboard with go-live checklist."""
        closed = self.closed_positions
        dash = PaperDashboard()

        if not closed:
            return dash

        winners = [p for p in closed if p.final_pnl is not None and p.final_pnl > 0]
        losers = [p for p in closed if p.final_pnl is not None and p.final_pnl <= 0]

        dash.total_trades = len(closed)
        dash.winners = len(winners)
        dash.losers = len(losers)
        dash.win_rate = len(winners) / len(closed)
        dash.total_pnl = sum(
            (p.final_pnl for p in closed if p.final_pnl is not None),
            Decimal("0"),
        )

        if winners:
            dash.avg_winner = sum(
                (p.final_pnl for p in winners if p.final_pnl is not None),
                Decimal("0"),
            ) / len(winners)
        if losers:
            total_losses = sum(
                (p.final_pnl for p in losers if p.final_pnl is not None),
                Decimal("0"),
            )
            dash.avg_loser = total_losses / len(losers)
            total_wins = sum(
                (p.final_pnl for p in winners if p.final_pnl is not None),
                Decimal("0"),
            )
            if total_losses != 0:
                dash.profit_factor = float(abs(total_wins / total_losses))

        dash.max_drawdown = self._max_drawdown()

        # Annualized return estimate
        first_close = min(
            (p.exit_time for p in closed if p.exit_time), default=None,
        )
        if first_close:
            days_elapsed = max(1, (datetime.utcnow() - first_close).days)
            dash.annualized_return = (
                float(dash.total_pnl / self.initial_capital)
                * (365 / days_elapsed)
            )

        # By conviction
        for level in ("high", "medium", "low"):
            trades = [p for p in closed if p.conviction == level]
            wins = [p for p in trades if p.final_pnl is not None and p.final_pnl > 0]
            count = len(trades)
            wr = len(wins) / count if count else 0.0
            if level == "high":
                dash.high_trades, dash.high_win_rate = count, wr
            elif level == "medium":
                dash.medium_trades, dash.medium_win_rate = count, wr
            else:
                dash.low_trades, dash.low_win_rate = count, wr

        # Go-live checklist
        loss_stops = len([
            p for p in closed
            if p.exit_reason and "loss_stop" in p.exit_reason
        ])

        dash.has_60_trades = dash.total_trades >= 60
        dash.has_55_win_rate = dash.win_rate >= 0.55
        dash.has_max_dd_under_12 = dash.max_drawdown < 0.12
        dash.has_high_wr_65 = dash.high_win_rate >= 0.65
        dash.has_loss_stops_3 = loss_stops >= 3

        checks = [
            dash.has_60_trades,
            dash.has_55_win_rate,
            dash.has_max_dd_under_12,
            dash.has_high_wr_65,
            dash.has_loss_stops_3,
        ]
        dash.checks_passed = sum(checks)
        dash.ready_for_live = all(checks)

        return dash

    def format_dashboard(self) -> str:
        """Format the dashboard as a Telegram-friendly string."""
        d = self.generate_dashboard()
        if d.total_trades == 0:
            return "No paper trades closed yet. Keep running."

        status = "READY FOR LIVE" if d.ready_for_live else (
            f"{d.checks_passed}/{d.checks_total} checks passed. Keep paper trading."
        )

        return (
            f"PAPER TRADING DASHBOARD\n\n"
            f"PERFORMANCE\n"
            f"  Trades: {d.total_trades} "
            f"({d.winners}W / {d.losers}L)\n"
            f"  Win rate: {d.win_rate:.1%}\n"
            f"  Total P&L: ${d.total_pnl:,.0f}\n"
            f"  Avg winner: ${d.avg_winner:,.0f}\n"
            f"  Avg loser: ${d.avg_loser:,.0f}\n"
            f"  Profit factor: {d.profit_factor:.2f}\n"
            f"  Max drawdown: {d.max_drawdown:.1%}\n"
            f"  Annualized: {d.annualized_return:.1%}\n\n"
            f"BY CONVICTION\n"
            f"  HIGH:   {d.high_trades} trades, "
            f"{d.high_win_rate:.0%} WR\n"
            f"  MEDIUM: {d.medium_trades} trades, "
            f"{d.medium_win_rate:.0%} WR\n"
            f"  LOW:    {d.low_trades} trades, "
            f"{d.low_win_rate:.0%} WR\n\n"
            f"GO-LIVE CHECKLIST\n"
            f"  {'[x]' if d.has_60_trades else '[ ]'} 60+ trades\n"
            f"  {'[x]' if d.has_55_win_rate else '[ ]'} Win rate >= 55%\n"
            f"  {'[x]' if d.has_max_dd_under_12 else '[ ]'} Max DD < 12%\n"
            f"  {'[x]' if d.has_high_wr_65 else '[ ]'} HIGH WR >= 65%\n"
            f"  {'[x]' if d.has_loss_stops_3 else '[ ]'} Loss stops triggered 3+\n\n"
            f"{status}"
        )

    # ── Internal helpers ────────────────────────────────────────

    def _max_drawdown(self) -> float:
        """Calculate max drawdown from closed trade equity curve."""
        closed = sorted(
            [p for p in self.closed_positions if p.exit_time],
            key=lambda x: x.exit_time or datetime.min,
        )
        if not closed:
            return 0.0

        peak = Decimal("0")
        max_dd = 0.0
        running = Decimal("0")

        for p in closed:
            running += p.final_pnl or Decimal("0")
            if running > peak:
                peak = running
            if peak > 0:
                dd = float((peak - running) / self.initial_capital)
                max_dd = max(max_dd, dd)

        return max_dd
