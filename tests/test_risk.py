"""Tests for the Risk & Tax module."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from src.models.account import AccountRouter, BrokerageAccount
from src.models.analysis import SizedOpportunity
from src.models.market import MarketContext
from src.models.position import Position, PortfolioState
from src.models.tax import TaxContext, TaxEngine, WashSaleTracker
from src.risk.account_routing import (
    check_liquidity_health,
    recommend_account,
)
from src.risk.drawdown import (
    DrawdownDecomposition,
    decompose_drawdown,
    format_drawdown_report,
)
from src.risk.greeks_guard import (
    PortfolioGreeksTargets,
    check_greeks_before_trade,
)
from src.risk.loss_mgmt import (
    LossManagementRules,
    evaluate_losing_position,
)
from src.risk.tax_alerts import generate_tax_alerts, generate_tax_section


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_portfolio() -> PortfolioState:
    return PortfolioState(
        net_liquidation=Decimal("1000000"),
        portfolio_delta=250.0,
        portfolio_vega=-500.0,
        sector_exposure={"technology": 0.25, "semiconductors": 0.10},
    )


@pytest.fixture
def sample_trade() -> SizedOpportunity:
    return SizedOpportunity(
        symbol="AAPL",
        trade_type="monthly_put",
        strike=Decimal("170"),
        expiration=date.today() + timedelta(days=35),
        premium=Decimal("3.50"),
        contracts=2,
        capital_deployed=Decimal("34000"),
        portfolio_pct=0.034,
        yield_on_capital=0.0103,
        annualized_yield=0.107,
        conviction="high",
    )


@pytest.fixture
def sample_position() -> Position:
    return Position(
        symbol="AAPL",
        position_type="short_put",
        quantity=-2,
        strike=Decimal("170"),
        expiration=date.today() + timedelta(days=30),
        entry_price=Decimal("3.50"),
        current_price=Decimal("5.00"),
        underlying_price=Decimal("168"),
        cost_basis=Decimal("0"),
        delta=-0.30,
        theta=0.05,
        gamma=0.02,
        vega=-0.15,
        iv=0.32,
        days_to_expiry=30,
        distance_from_strike_pct=-1.2,
    )


@pytest.fixture
def sample_mkt() -> MarketContext:
    return MarketContext(
        symbol="AAPL",
        iv_rank=55.0,
        iv_percentile=60.0,
        iv_rank_change_5d=5.0,
        iv_30d=0.30,
        hv_30d=0.25,
        iv_hv_spread=0.05,
        price=Decimal("168"),
        price_change_1d=-1.5,
        price_change_5d=-3.0,
        price_vs_52w_high=-12.0,
        price_vs_200sma=-2.0,
        put_call_ratio=1.2,
        option_volume_vs_avg=1.5,
        vix=22.0,
    )


@pytest.fixture
def router() -> AccountRouter:
    return AccountRouter(
        accounts={
            "roth": BrokerageAccount(
                account_id="roth",
                account_type="roth_ira",
                total_value=Decimal("300000"),
                buying_power=Decimal("50000"),
                options_level=2,
                withdrawal_restricted=True,
                roth_contribution_basis=Decimal("100000"),
            ),
            "taxable": BrokerageAccount(
                account_id="taxable",
                account_type="taxable",
                total_value=Decimal("500000"),
                buying_power=Decimal("200000"),
                options_level=4,
            ),
            "trad_ira": BrokerageAccount(
                account_id="trad_ira",
                account_type="traditional_ira",
                total_value=Decimal("200000"),
                buying_power=Decimal("30000"),
                options_level=2,
                withdrawal_restricted=True,
            ),
        },
        monthly_expenses=Decimal("8000"),
    )


# ---------------------------------------------------------------------------
# Greeks Guard
# ---------------------------------------------------------------------------

class TestGreeksGuard:
    def test_trade_within_range(
        self, sample_trade: SizedOpportunity, sample_portfolio: PortfolioState
    ) -> None:
        allowed, reason = check_greeks_before_trade(
            sample_trade, sample_portfolio, regime="attack"
        )
        assert allowed
        assert "within range" in reason.lower()

    def test_delta_exceeds_range(
        self, sample_trade: SizedOpportunity, sample_portfolio: PortfolioState
    ) -> None:
        sample_portfolio.portfolio_delta = 460.0  # +50 from trade = 510 > 500
        allowed, reason = check_greeks_before_trade(
            sample_trade, sample_portfolio, regime="attack"
        )
        assert not allowed
        assert "delta" in reason.lower()

    def test_defend_regime_tighter(
        self, sample_trade: SizedOpportunity, sample_portfolio: PortfolioState
    ) -> None:
        sample_portfolio.portfolio_delta = 200.0
        allowed, _ = check_greeks_before_trade(
            sample_trade, sample_portfolio, regime="defend"
        )
        assert not allowed

    def test_vega_cap(
        self, sample_trade: SizedOpportunity, sample_portfolio: PortfolioState
    ) -> None:
        sample_portfolio.portfolio_vega = -19900.0  # +170 from trade = 20070 > 20000 cap
        allowed, reason = check_greeks_before_trade(
            sample_trade, sample_portfolio, regime="attack"
        )
        assert not allowed
        assert "vega" in reason.lower()

    def test_beta_check(
        self, sample_trade: SizedOpportunity, sample_portfolio: PortfolioState
    ) -> None:
        sample_portfolio.sector_exposure = {"technology": 0.70, "semiconductors": 0.40}
        allowed, reason = check_greeks_before_trade(
            sample_trade, sample_portfolio, regime="attack"
        )
        assert not allowed
        assert "beta" in reason.lower()


# ---------------------------------------------------------------------------
# Loss Management
# ---------------------------------------------------------------------------

class TestLossManagement:
    def test_hold_otm(
        self, sample_position: Position, sample_mkt: MarketContext
    ) -> None:
        sample_position.distance_from_strike_pct = 3.0
        sample_position.current_price = Decimal("4.00")
        action, reason = evaluate_losing_position(sample_position, sample_mkt)
        assert action == "HOLD"
        assert "theta" in reason.lower()

    def test_loss_stop_triggered(
        self, sample_position: Position, sample_mkt: MarketContext
    ) -> None:
        sample_position.current_price = Decimal("7.50")  # > 2x of 3.50
        action, reason = evaluate_losing_position(sample_position, sample_mkt)
        assert action == "CLOSE_LOSS"
        assert "loss stop" in reason.lower()

    def test_weekly_tighter_stop(
        self, sample_position: Position, sample_mkt: MarketContext
    ) -> None:
        sample_position.days_to_expiry = 7
        sample_position.current_price = Decimal("5.60")  # 1.6x — above 1.5x weekly
        action, _ = evaluate_losing_position(sample_position, sample_mkt)
        assert action == "CLOSE_LOSS"

    def test_underlying_crash_stop(
        self, sample_position: Position, sample_mkt: MarketContext
    ) -> None:
        # underlying 16% below strike
        sample_position.underlying_price = Decimal("142.80")
        action, reason = evaluate_losing_position(sample_position, sample_mkt)
        assert action == "CLOSE_LOSS"
        assert "crash" in reason.lower()

    def test_itm_roll(
        self, sample_position: Position, sample_mkt: MarketContext
    ) -> None:
        sample_position.distance_from_strike_pct = -2.0
        sample_position.current_price = Decimal("5.00")  # < 2x
        sample_position.days_to_expiry = 25
        action, reason = evaluate_losing_position(sample_position, sample_mkt)
        assert action == "ROLL"
        assert "roll" in reason.lower()

    def test_itm_near_expiry_assignment(
        self, sample_position: Position, sample_mkt: MarketContext
    ) -> None:
        sample_position.distance_from_strike_pct = -2.0
        sample_position.current_price = Decimal("5.00")
        sample_position.days_to_expiry = 5
        action, reason = evaluate_losing_position(sample_position, sample_mkt)
        assert action == "TAKE_ASSIGNMENT"

    def test_approaching_stop_warning(
        self, sample_position: Position, sample_mkt: MarketContext
    ) -> None:
        sample_position.current_price = Decimal("6.00")  # 1.71x
        sample_position.distance_from_strike_pct = 1.0
        action, reason = evaluate_losing_position(sample_position, sample_mkt)
        assert action == "HOLD"
        assert "approaching" in reason.lower()


# ---------------------------------------------------------------------------
# Drawdown Decomposition
# ---------------------------------------------------------------------------

class TestDrawdown:
    def test_basic_decomposition(self) -> None:
        dd = decompose_drawdown(
            losing_trades=[
                {"symbol": "AAPL", "loss_dollars": 5000},
                {"symbol": "NVDA", "loss_dollars": 15000},
            ],
            peak_nlv=Decimal("1000000"),
            trough_nlv=Decimal("940000"),
            spy_move_pct=-3.0,
            vix_change=8.0,
            portfolio_beta=1.2,
            portfolio_vega=-2000.0,
        )
        assert dd.total_drawdown_pct == pytest.approx(0.06, abs=0.001)
        assert dd.total_drawdown_dollars == Decimal("60000")
        assert dd.primary_cause in ("single_blowup", "correlation", "vega")

    def test_correlation_dominant(self) -> None:
        dd = decompose_drawdown(
            losing_trades=[{"symbol": "SPY", "loss_dollars": 1000}],
            peak_nlv=Decimal("1000000"),
            trough_nlv=Decimal("950000"),
            spy_move_pct=-5.0,
            vix_change=2.0,
            portfolio_beta=1.5,
            portfolio_vega=-100.0,
        )
        assert dd.primary_cause == "correlation"

    def test_single_blowup_dominant(self) -> None:
        dd = decompose_drawdown(
            losing_trades=[{"symbol": "MEME", "loss_dollars": 45000}],
            peak_nlv=Decimal("1000000"),
            trough_nlv=Decimal("950000"),
            spy_move_pct=-0.001,  # tiny market move
            vix_change=0.01,     # tiny IV change
            portfolio_beta=1.0,
            portfolio_vega=-100.0,
        )
        assert dd.primary_cause == "single_blowup"

    def test_zero_drawdown(self) -> None:
        dd = decompose_drawdown(
            losing_trades=[],
            peak_nlv=Decimal("1000000"),
            trough_nlv=Decimal("1000000"),
            spy_move_pct=0.0,
            vix_change=0.0,
            portfolio_beta=1.0,
            portfolio_vega=0.0,
        )
        assert dd.total_drawdown_pct == 0.0

    def test_format_report(self) -> None:
        dd = decompose_drawdown(
            losing_trades=[{"symbol": "AAPL", "loss_dollars": 10000}],
            peak_nlv=Decimal("1000000"),
            trough_nlv=Decimal("950000"),
            spy_move_pct=-3.0,
            vix_change=5.0,
            portfolio_beta=1.2,
            portfolio_vega=-500.0,
        )
        report = format_drawdown_report(dd)
        assert "DRAWDOWN ANALYSIS" in report
        assert "PRIMARY CAUSE" in report
        assert "ACTION" in report


# ---------------------------------------------------------------------------
# Tax Alerts
# ---------------------------------------------------------------------------

class TestTaxAlerts:
    def test_wash_sale_alert(self) -> None:
        tracker = WashSaleTracker()
        tracker.record_loss("AAPL", date.today() - timedelta(days=10), Decimal("500"))
        alerts = generate_tax_alerts([], tracker)
        assert any("wash sale" in a.lower() for a in alerts)

    def test_ltcg_approaching(self) -> None:
        ctx = TaxContext(
            symbol="NVDA",
            cost_basis_per_share=Decimal("100"),
            current_price=Decimal("200"),
            unrealized_gain=Decimal("10000"),
            unrealized_gain_pct=100.0,
            purchase_date=date.today() - timedelta(days=320),
            holding_period_days=320,
            is_ltcg=False,
            days_until_ltcg=45,
            tax_savings_by_waiting=Decimal("1700"),
        )
        alerts = generate_tax_alerts([ctx])
        assert any("ltcg" in a.lower() for a in alerts)

    def test_stcg_exposure(self) -> None:
        ctx = TaxContext(
            symbol="MSFT",
            cost_basis_per_share=Decimal("300"),
            current_price=Decimal("380"),
            unrealized_gain=Decimal("8000"),
            unrealized_gain_pct=26.7,
            purchase_date=date.today() - timedelta(days=100),
            holding_period_days=100,
            is_ltcg=False,
            estimated_tax_if_sold=Decimal("3264"),
        )
        alerts = generate_tax_alerts([ctx])
        assert any("stcg" in a.lower() for a in alerts)

    def test_tax_loss_harvest(self) -> None:
        ctx = TaxContext(
            symbol="META",
            cost_basis_per_share=Decimal("400"),
            current_price=Decimal("350"),
            unrealized_gain=Decimal("-5000"),
            unrealized_gain_pct=-12.5,
            purchase_date=date.today() - timedelta(days=60),
            holding_period_days=60,
            is_ltcg=False,
        )
        alerts = generate_tax_alerts([ctx])
        assert any("harvest" in a.lower() for a in alerts)

    def test_no_harvest_during_wash_window(self) -> None:
        tracker = WashSaleTracker()
        tracker.record_loss("META", date.today() - timedelta(days=5), Decimal("1000"))
        ctx = TaxContext(
            symbol="META",
            cost_basis_per_share=Decimal("400"),
            current_price=Decimal("350"),
            unrealized_gain=Decimal("-5000"),
            unrealized_gain_pct=-12.5,
            purchase_date=date.today() - timedelta(days=60),
            holding_period_days=60,
            is_ltcg=False,
        )
        alerts = generate_tax_alerts([ctx], tracker)
        # Should have wash sale alert but NOT harvest recommendation
        assert any("wash sale" in a.lower() for a in alerts)
        assert not any("harvest" in a.lower() for a in alerts)

    def test_generate_tax_section(self) -> None:
        engine = TaxEngine(
            realized_stcg_ytd=Decimal("15000"),
            realized_ltcg_ytd=Decimal("5000"),
            realized_losses_ytd=Decimal("3000"),
            option_premium_income_ytd=Decimal("20000"),
            harvested_losses_ytd=Decimal("2000"),
        )
        section = generate_tax_section(engine, [])
        assert "YTD STCG" in section
        assert "Estimated tax" in section
        assert "quarterly" in section.lower()


# ---------------------------------------------------------------------------
# Account Routing
# ---------------------------------------------------------------------------

class TestAccountRouting:
    def test_puts_route_to_roth(
        self, router: AccountRouter, sample_trade: SizedOpportunity
    ) -> None:
        acct_id, reason = recommend_account(router, sample_trade)
        assert acct_id == "roth"
        assert "roth" in reason.lower()

    def test_strangle_routes_to_taxable(
        self, router: AccountRouter, sample_trade: SizedOpportunity
    ) -> None:
        sample_trade.trade_type = "strangle"
        acct_id, reason = recommend_account(router, sample_trade)
        assert acct_id == "taxable"

    def test_no_eligible_account(
        self, router: AccountRouter, sample_trade: SizedOpportunity
    ) -> None:
        sample_trade.capital_deployed = Decimal("999999999")
        acct_id, reason = recommend_account(router, sample_trade)
        assert acct_id == ""
        assert "no eligible" in reason.lower()

    def test_insufficient_options_level(self, router: AccountRouter) -> None:
        trade = SizedOpportunity(
            symbol="AAPL",
            trade_type="strangle",
            strike=Decimal("170"),
            expiration=date.today() + timedelta(days=35),
            premium=Decimal("3.50"),
            contracts=2,
            capital_deployed=Decimal("34000"),
            portfolio_pct=0.034,
            yield_on_capital=0.0103,
            annualized_yield=0.107,
            conviction="high",
        )
        # Only taxable has Level 4
        acct_id, _ = recommend_account(router, trade)
        assert acct_id == "taxable"

    def test_tax_savings_estimate(
        self, router: AccountRouter, sample_trade: SizedOpportunity
    ) -> None:
        _, reason = recommend_account(router, sample_trade)
        assert "saves" in reason.lower()


class TestLiquidityHealth:
    def test_healthy_portfolio(self, router: AccountRouter) -> None:
        healthy, msg = check_liquidity_health(router)
        assert healthy
        assert "healthy" in msg.lower()

    def test_low_liquidity_ratio(self) -> None:
        router = AccountRouter(
            accounts={
                "trad_ira": BrokerageAccount(
                    account_id="trad_ira",
                    account_type="traditional_ira",
                    total_value=Decimal("800000"),
                    withdrawal_restricted=True,
                ),
                "taxable": BrokerageAccount(
                    account_id="taxable",
                    account_type="taxable",
                    total_value=Decimal("100000"),
                ),
            },
        )
        healthy, msg = check_liquidity_health(router)
        assert not healthy
        assert "ratio" in msg.lower() or "liquid" in msg.lower()

    def test_restricted_account_too_large(self) -> None:
        router = AccountRouter(
            accounts={
                "trad_ira": BrokerageAccount(
                    account_id="trad_ira",
                    account_type="traditional_ira",
                    total_value=Decimal("600000"),
                    withdrawal_restricted=True,
                ),
                "taxable": BrokerageAccount(
                    account_id="taxable",
                    account_type="taxable",
                    total_value=Decimal("400000"),
                ),
            },
        )
        healthy, msg = check_liquidity_health(router)
        # trad_ira is 60% of NLV (> 40% threshold)
        assert not healthy
        assert "trad_ira" in msg

    def test_below_emergency_reserve(self) -> None:
        router = AccountRouter(
            accounts={
                "taxable": BrokerageAccount(
                    account_id="taxable",
                    account_type="taxable",
                    total_value=Decimal("30000"),
                ),
            },
            monthly_expenses=Decimal("10000"),
            emergency_reserve_months=6,
        )
        healthy, msg = check_liquidity_health(router)
        assert not healthy
        assert "emergency" in msg.lower() or "liquid" in msg.lower()
