"""Database repository — async CRUD for all tables.

Uses a connection pool interface. In production: asyncpg.
For testing and paper trading: sqlite3 (synchronous wrapper).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol


class DBConnection(Protocol):
    """Database connection interface."""
    async def execute(self, query: str, *args: Any) -> None: ...
    async def fetch_one(self, query: str, *args: Any) -> dict[str, Any] | None: ...
    async def fetch_all(self, query: str, *args: Any) -> list[dict[str, Any]]: ...


# ---------------------------------------------------------------------------
# In-memory store for paper trading and testing (no external DB needed)
# ---------------------------------------------------------------------------

class InMemoryDB:
    """In-memory database for paper trading and testing."""

    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {}
        self._next_id: dict[str, int] = {}

    def _get_table(self, name: str) -> list[dict[str, Any]]:
        if name not in self._tables:
            self._tables[name] = []
            self._next_id[name] = 1
        return self._tables[name]

    async def execute(self, query: str, *args: Any) -> None:
        pass  # No-op for DDL

    async def fetch_one(self, query: str, *args: Any) -> dict[str, Any] | None:
        return None

    async def fetch_all(self, query: str, *args: Any) -> list[dict[str, Any]]:
        return []

    def insert(self, table: str, row: dict[str, Any]) -> int:
        """Insert a row and return its ID."""
        rows = self._get_table(table)
        row_id = self._next_id[table]
        self._next_id[table] += 1
        row["id"] = row_id
        row.setdefault("created_at", datetime.utcnow())
        rows.append(row)
        return row_id

    def find(
        self, table: str, **filters: Any
    ) -> list[dict[str, Any]]:
        """Find rows matching all filters."""
        rows = self._get_table(table)
        result = []
        for row in rows:
            if all(row.get(k) == v for k, v in filters.items()):
                result.append(row)
        return result

    def find_one(
        self, table: str, **filters: Any
    ) -> dict[str, Any] | None:
        """Find first row matching filters."""
        results = self.find(table, **filters)
        return results[0] if results else None

    def update(
        self, table: str, row_id: int, **updates: Any
    ) -> bool:
        """Update a row by ID."""
        rows = self._get_table(table)
        for row in rows:
            if row.get("id") == row_id:
                row.update(updates)
                return True
        return False

    def count(self, table: str, **filters: Any) -> int:
        """Count rows matching filters."""
        return len(self.find(table, **filters))

    def all(self, table: str) -> list[dict[str, Any]]:
        """Return all rows in a table."""
        return list(self._get_table(table))


# ---------------------------------------------------------------------------
# Repository functions (work with InMemoryDB or real DB)
# ---------------------------------------------------------------------------

@dataclass
class TradeRepository:
    """Repository for trade tracking operations."""
    db: InMemoryDB

    def log_recommendation(
        self,
        symbol: str,
        action_type: str,
        strike: Decimal,
        expiration: date | None,
        premium: Decimal,
        contracts: int,
        conviction: str,
        strategy: str,
        signals: list[str],
        reasoning: str,
    ) -> int:
        """Log a trade recommendation."""
        return self.db.insert("recommendations", {
            "date": date.today(),
            "symbol": symbol,
            "action_type": action_type,
            "strike": strike,
            "expiration": expiration,
            "premium_target": premium,
            "contracts": contracts,
            "conviction": conviction,
            "strategy": strategy,
            "signals": signals,
            "reasoning": reasoning,
        })

    def log_execution(
        self,
        recommendation_id: int,
        price: Decimal,
        account_id: str,
        slippage: Decimal = Decimal("0"),
        fees: Decimal = Decimal("0"),
    ) -> int:
        """Log a trade execution."""
        return self.db.insert("executions", {
            "recommendation_id": recommendation_id,
            "executed": True,
            "execution_price": price,
            "execution_time": datetime.utcnow(),
            "slippage": slippage,
            "fees": fees,
            "account_id": account_id,
        })

    def log_paper_trade(
        self,
        symbol: str,
        trade_type: str,
        strike: Decimal,
        expiration: date | None,
        contracts: int,
        conviction: str,
        strategy: str,
        entry_price: Decimal,
        entry_underlying: Decimal,
        iv_rank: float,
        capital_at_risk: Decimal,
    ) -> int:
        """Log a paper trade entry."""
        return self.db.insert("paper_trades", {
            "symbol": symbol,
            "trade_type": trade_type,
            "strike": strike,
            "expiration": expiration,
            "contracts": contracts,
            "conviction": conviction,
            "strategy": strategy,
            "entry_price": entry_price,
            "entry_time": datetime.utcnow(),
            "entry_underlying": entry_underlying,
            "entry_iv_rank": iv_rank,
            "capital_at_risk": capital_at_risk,
            "exit_price": None,
            "pnl": None,
        })

    def close_paper_trade(
        self,
        trade_id: int,
        exit_price: Decimal,
        exit_underlying: Decimal,
        exit_reason: str,
        pnl: Decimal,
        pnl_pct: float,
    ) -> bool:
        """Close a paper trade with exit details."""
        return self.db.update("paper_trades", trade_id,
            exit_price=exit_price,
            exit_time=datetime.utcnow(),
            exit_underlying=exit_underlying,
            exit_reason=exit_reason,
            pnl=pnl,
            pnl_pct=pnl_pct,
        )

    def get_open_paper_trades(self) -> list[dict[str, Any]]:
        """Get all open paper trades."""
        return self.db.find("paper_trades", exit_price=None)

    def get_closed_paper_trades(self) -> list[dict[str, Any]]:
        """Get all closed paper trades."""
        all_trades = self.db.all("paper_trades")
        return [t for t in all_trades if t.get("exit_price") is not None]


@dataclass
class SnapshotRepository:
    """Repository for daily snapshots."""
    db: InMemoryDB

    def log_snapshot(
        self,
        nlv: Decimal,
        theta: Decimal,
        delta: float,
        positions: int,
        signals_fired: int,
        trades_executed: int,
        regime: str,
        vix: float,
    ) -> int:
        """Log a daily portfolio snapshot."""
        return self.db.insert("daily_snapshots", {
            "date": date.today(),
            "net_liquidation": nlv,
            "daily_theta": theta,
            "portfolio_delta": Decimal(str(delta)),
            "num_positions": positions,
            "num_signals_fired": signals_fired,
            "num_trades_executed": trades_executed,
            "regime": regime,
            "vix_close": Decimal(str(vix)),
        })

    def get_snapshot(self, target_date: date) -> dict[str, Any] | None:
        """Get snapshot for a specific date."""
        return self.db.find_one("daily_snapshots", date=target_date)

    def get_recent_snapshots(self, days: int = 30) -> list[dict[str, Any]]:
        """Get recent snapshots."""
        all_snaps = self.db.all("daily_snapshots")
        all_snaps.sort(key=lambda s: s.get("date", date.min), reverse=True)
        return all_snaps[:days]


@dataclass
class LearningRepository:
    """Repository for learning loop parameter adjustments."""
    db: InMemoryDB

    def log_adjustment(
        self,
        review_type: str,
        param_name: str,
        old_value: float,
        new_value: float,
        reason: str,
    ) -> int:
        """Log a parameter adjustment."""
        return self.db.insert("parameter_adjustments", {
            "adjustment_date": date.today(),
            "review_type": review_type,
            "param_name": param_name,
            "old_value": Decimal(str(old_value)),
            "new_value": Decimal(str(new_value)),
            "reason": reason,
            "approved": True,
        })

    def log_signal_performance(
        self,
        signal_type: str,
        trade_count: int,
        win_rate: float,
        avg_return: float,
        sharpe: float,
    ) -> int:
        """Log weekly signal performance."""
        return self.db.insert("signal_performance_history", {
            "week_ending": date.today(),
            "signal_type": signal_type,
            "trade_count": trade_count,
            "win_rate": Decimal(str(win_rate)),
            "avg_return": Decimal(str(avg_return)),
            "sharpe_ratio": Decimal(str(sharpe)),
        })

    def get_adjustment_history(
        self, param_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get parameter adjustment history."""
        if param_name:
            return self.db.find("parameter_adjustments", param_name=param_name)
        return self.db.all("parameter_adjustments")


@dataclass
class WashSaleRepository:
    """Repository for wash sale tracking."""
    db: InMemoryDB

    def record_loss(
        self, symbol: str, loss_date: date, loss_amount: Decimal,
    ) -> int:
        """Record a loss close event."""
        from datetime import timedelta
        window_end = loss_date + timedelta(days=30)
        return self.db.insert("wash_sale_tracker", {
            "symbol": symbol,
            "loss_date": loss_date,
            "loss_amount": loss_amount,
            "wash_sale_window_end": window_end,
            "is_active": True,
        })

    def get_active_windows(self) -> list[dict[str, Any]]:
        """Get all active wash sale windows."""
        return self.db.find("wash_sale_tracker", is_active=True)

    def is_blocked(self, symbol: str) -> bool:
        """Check if a symbol is blocked by wash sale window."""
        active = self.db.find("wash_sale_tracker", symbol=symbol, is_active=True)
        today = date.today()
        return any(
            row.get("wash_sale_window_end", date.min) >= today
            for row in active
        )
