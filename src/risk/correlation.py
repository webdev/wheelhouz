"""Portfolio correlation tracking — cluster detection and crisis overrides.

Detects correlated position clusters and enforces concentration limits.
During crisis (VIX > 30), assumes 0.95 correlation across all tech/semi.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


# Sector classification for correlation grouping
SECTOR_MAP: dict[str, str] = {
    "AAPL": "technology", "MSFT": "technology", "GOOGL": "technology",
    "META": "technology", "AMZN": "technology", "ADBE": "technology",
    "CRM": "technology", "NOW": "technology", "ORCL": "technology",
    "NVDA": "semiconductors", "AMD": "semiconductors", "AVGO": "semiconductors",
    "QCOM": "semiconductors", "MU": "semiconductors", "TSM": "semiconductors",
    "INTC": "semiconductors", "MRVL": "semiconductors",
}

# Sectors that become highly correlated during crisis
CRISIS_CORRELATED_SECTORS = {"technology", "semiconductors"}
CRISIS_CORRELATION = 0.95


@dataclass
class CorrelationCluster:
    """A group of correlated positions."""
    sector: str
    symbols: list[str]
    total_exposure_pct: float  # % of NLV
    effective_exposure_pct: float  # correlation-adjusted
    is_over_limit: bool = False


@dataclass
class CorrelationReport:
    """Portfolio-level correlation analysis."""
    clusters: list[CorrelationCluster] = field(default_factory=list)
    max_single_name_pct: float = 0.0
    max_single_name: str = ""
    max_sector_pct: float = 0.0
    max_sector: str = ""
    crisis_mode: bool = False
    warnings: list[str] = field(default_factory=list)


def analyze_correlation(
    positions: dict[str, Decimal],
    nlv: Decimal,
    vix: float,
    max_single_name_pct: float = 0.10,
    max_sector_pct: float = 0.35,
) -> CorrelationReport:
    """Analyze portfolio correlation and concentration risk.

    positions: {symbol: market_value}
    During crisis (VIX > 30): treat all tech/semi as one cluster at 0.95 correlation.
    """
    report = CorrelationReport()
    if nlv == 0:
        return report

    crisis_mode = vix > 30
    report.crisis_mode = crisis_mode

    # Single-name concentration
    for symbol, value in positions.items():
        pct = float(value / nlv)
        if pct > report.max_single_name_pct:
            report.max_single_name_pct = pct
            report.max_single_name = symbol
        if pct > max_single_name_pct:
            report.warnings.append(
                f"{symbol}: {pct:.1%} of NLV (limit {max_single_name_pct:.0%})"
            )

    # Sector clustering
    by_sector: dict[str, list[tuple[str, float]]] = {}
    for symbol, value in positions.items():
        sector = SECTOR_MAP.get(symbol, "other")
        pct = float(value / nlv)
        by_sector.setdefault(sector, []).append((symbol, pct))

    for sector, members in by_sector.items():
        total_pct = sum(pct for _, pct in members)
        symbols = [s for s, _ in members]

        # In crisis: correlated sectors get effective exposure multiplied
        if crisis_mode and sector in CRISIS_CORRELATED_SECTORS:
            effective = total_pct * CRISIS_CORRELATION
        else:
            effective = total_pct * 0.6  # normal diversification benefit

        is_over = total_pct > max_sector_pct
        cluster = CorrelationCluster(
            sector=sector,
            symbols=symbols,
            total_exposure_pct=total_pct,
            effective_exposure_pct=effective,
            is_over_limit=is_over,
        )
        report.clusters.append(cluster)

        if total_pct > report.max_sector_pct:
            report.max_sector_pct = total_pct
            report.max_sector = sector

        if is_over:
            report.warnings.append(
                f"{sector}: {total_pct:.1%} of NLV (limit {max_sector_pct:.0%})"
            )

    # Crisis correlation warning
    if crisis_mode:
        tech_semi_pct = sum(
            c.total_exposure_pct for c in report.clusters
            if c.sector in CRISIS_CORRELATED_SECTORS
        )
        if tech_semi_pct > 0.30:
            report.warnings.append(
                f"CRISIS CORRELATION: tech+semi = {tech_semi_pct:.0%} of NLV. "
                f"Treat as single {CRISIS_CORRELATION:.0%}-correlated position."
            )

    # Sort clusters by exposure
    report.clusters.sort(key=lambda c: c.total_exposure_pct, reverse=True)
    return report


def would_increase_concentration(
    symbol: str,
    trade_value: Decimal,
    positions: dict[str, Decimal],
    nlv: Decimal,
    max_single_name_pct: float = 0.10,
    max_sector_pct: float = 0.35,
) -> tuple[bool, str]:
    """Check if adding a trade would violate concentration limits.

    Returns (would_violate, reason).
    """
    if nlv == 0:
        return (True, "NLV is zero")

    # Single name check
    current = positions.get(symbol, Decimal("0"))
    new_pct = float((current + trade_value) / nlv)
    if new_pct > max_single_name_pct:
        return (
            True,
            f"{symbol} would be {new_pct:.1%} of NLV (limit {max_single_name_pct:.0%})",
        )

    # Sector check
    sector = SECTOR_MAP.get(symbol, "other")
    sector_total = sum(
        float(v / nlv)
        for s, v in positions.items()
        if SECTOR_MAP.get(s, "other") == sector
    )
    new_sector = sector_total + float(trade_value / nlv)
    if new_sector > max_sector_pct:
        return (
            True,
            f"{sector} sector would be {new_sector:.1%} of NLV "
            f"(limit {max_sector_pct:.0%})",
        )

    return (False, "Within limits")


def format_correlation_report(report: CorrelationReport) -> str:
    """Format correlation report for briefing."""
    lines = ["CORRELATION ANALYSIS"]
    if report.crisis_mode:
        lines.append("  !! CRISIS MODE: assuming 0.95 tech/semi correlation")

    lines.append(f"  Largest position: {report.max_single_name} "
                 f"({report.max_single_name_pct:.1%})")
    lines.append(f"  Largest sector: {report.max_sector} "
                 f"({report.max_sector_pct:.1%})")

    if report.warnings:
        lines.append("\n  WARNINGS:")
        for w in report.warnings:
            lines.append(f"    ! {w}")
    else:
        lines.append("  No concentration violations.")

    return "\n".join(lines)
