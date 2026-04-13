"""Sample portfolio positions for testing."""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from src.models.position import PortfolioState, Position


def make_position(**overrides: Any) -> Position:
    """Create a Position with sensible defaults. Override any field."""
    defaults: dict[str, Any] = {
        "symbol": "NVDA",
        "position_type": "short_put",
        "quantity": 1,
        "strike": Decimal("800.00"),
        "expiration": date.today() + timedelta(days=30),
        "entry_price": Decimal("12.50"),
        "current_price": Decimal("6.25"),
        "underlying_price": Decimal("875.00"),
        "cost_basis": Decimal("0"),
        "delta": -0.25,
        "theta": 0.45,
        "gamma": 0.002,
        "vega": 0.85,
        "iv": 0.42,
        "days_to_expiry": 30,
        "distance_from_strike_pct": 9.4,
        "profit_pct": 0.50,
        "max_profit": Decimal("1250.00"),
        "max_loss": Decimal("78750.00"),
        "account_id": "taxable_001",
        "engine": "engine2",
        "option_type": "put",
        "capital_at_risk": Decimal("80000.00"),
        "current_profit": Decimal("625.00"),
        "purchase_date": date.today() - timedelta(days=15),
        "holding_period_days": 15,
        "unrealized_pnl": Decimal("625.00"),
        "market_value": Decimal("625.00"),
    }
    defaults.update(overrides)
    return Position(**defaults)


def make_portfolio_state(**overrides: Any) -> PortfolioState:
    """Create a PortfolioState with sensible defaults."""
    defaults: dict[str, Any] = {
        "positions": [
            make_position(symbol="NVDA", strike=Decimal("800.00")),
            make_position(symbol="AAPL", strike=Decimal("170.00"),
                          underlying_price=Decimal("185.00"),
                          entry_price=Decimal("3.20"),
                          current_price=Decimal("1.60")),
            make_position(symbol="ADBE", position_type="long_stock",
                          strike=Decimal("0"), quantity=200,
                          entry_price=Decimal("450.00"),
                          current_price=Decimal("480.00"),
                          underlying_price=Decimal("480.00"),
                          cost_basis=Decimal("90000.00"),
                          delta=1.0, theta=0.0, gamma=0.0, vega=0.0, iv=0.0,
                          engine="engine1"),
        ],
        "cash_available": Decimal("95000.00"),
        "buying_power": Decimal("190000.00"),
        "net_liquidation": Decimal("1000000.00"),
        "portfolio_delta": 350.0,
        "portfolio_theta": 125.0,
        "portfolio_vega": -45.0,
        "concentration": {"NVDA": 0.08, "AAPL": 0.05, "ADBE": 0.096},
        "sector_exposure": {"technology": 0.32, "semiconductors": 0.15},
        "margin_utilization": 0.35,
    }
    defaults.update(overrides)
    return PortfolioState(**defaults)


# Pre-built fixtures for common test scenarios
SAMPLE_SHORT_PUT = make_position()

SAMPLE_SHORT_CALL = make_position(
    position_type="short_call",
    symbol="MSFT",
    strike=Decimal("430.00"),
    underlying_price=Decimal("410.00"),
    entry_price=Decimal("5.80"),
    current_price=Decimal("2.90"),
    delta=0.20,
    option_type="call",
    distance_from_strike_pct=4.9,
    profit_pct=0.50,
)

SAMPLE_LONG_STOCK = make_position(
    position_type="long_stock",
    symbol="ADBE",
    quantity=200,
    strike=Decimal("0"),
    expiration=None,
    entry_price=Decimal("450.00"),
    current_price=Decimal("480.00"),
    underlying_price=Decimal("480.00"),
    cost_basis=Decimal("90000.00"),
    delta=1.0,
    theta=0.0,
    gamma=0.0,
    vega=0.0,
    iv=0.0,
    days_to_expiry=0,
    engine="engine1",
    holding_period_days=400,
    purchase_date=date.today() - timedelta(days=400),
)

SAMPLE_PORTFOLIO = make_portfolio_state()
