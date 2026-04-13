"""Risk & Tax module — Greeks guard, loss management, drawdown, tax alerts, routing."""

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
from src.risk.tax_alerts import (
    generate_tax_alerts,
    generate_tax_section,
)

__all__ = [
    "check_liquidity_health",
    "recommend_account",
    "DrawdownDecomposition",
    "decompose_drawdown",
    "format_drawdown_report",
    "PortfolioGreeksTargets",
    "check_greeks_before_trade",
    "LossManagementRules",
    "evaluate_losing_position",
    "generate_tax_alerts",
    "generate_tax_section",
]
