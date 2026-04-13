"""Risk & Tax module — Greeks guard, loss management, drawdown, tax, routing, correlation."""

from src.risk.account_routing import (
    check_liquidity_health,
    recommend_account,
)
from src.risk.correlation import (
    CorrelationReport,
    analyze_correlation,
    format_correlation_report,
    would_increase_concentration,
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
from src.risk.vesting import (
    VestingTracker,
    check_employer_emergency,
    format_vesting_summary,
)

__all__ = [
    "check_liquidity_health",
    "recommend_account",
    "CorrelationReport",
    "analyze_correlation",
    "format_correlation_report",
    "would_increase_concentration",
    "DrawdownDecomposition",
    "decompose_drawdown",
    "format_drawdown_report",
    "PortfolioGreeksTargets",
    "check_greeks_before_trade",
    "LossManagementRules",
    "evaluate_losing_position",
    "generate_tax_alerts",
    "generate_tax_section",
    "VestingTracker",
    "check_employer_emergency",
    "format_vesting_summary",
]
