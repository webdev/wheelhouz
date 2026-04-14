# src/models/intelligence.py
"""Intelligence mesh models — unified context for multi-source reasoning."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from src.models.market import EventCalendar, MarketContext
from src.models.position import Position
from src.models.signals import AlphaSignal
from src.models.analysis import SmartStrike


@dataclass
class QuantIntelligence:
    """Quantitative signal intelligence for a symbol."""
    signals: list[AlphaSignal]
    signal_count: int
    avg_strength: float
    iv_rank: float
    iv_percentile: float
    rsi: float | None
    price_vs_support: dict[str, float]
    trend_direction: str  # "uptrend" / "downtrend" / "range"


@dataclass
class TechnicalConsensus:
    """TradingView technical analysis consensus."""
    source: str  # "tradingview"
    overall: str  # STRONG_BUY / BUY / NEUTRAL / SELL / STRONG_SELL
    oscillators: str
    moving_averages: str
    buy_count: int
    neutral_count: int
    sell_count: int
    raw_indicators: dict[str, float] = field(default_factory=dict)


@dataclass
class OptionsIntelligence:
    """Real options chain intelligence for a symbol."""
    best_strike: SmartStrike | None
    iv_rank: float
    premium_yield: float
    annualized_yield: float
    bid_ask_spread_pct: float
    chain_available: bool


@dataclass
class PortfolioContext:
    """Portfolio-level context for a symbol."""
    existing_exposure_pct: float
    existing_positions: list[Position]
    account_recommendation: str
    wash_sale_blocked: bool
    earnings_conflict: bool
    available_capital: Decimal


@dataclass
class IntelligenceContext:
    """Unified intelligence context — one per symbol per analysis cycle."""
    symbol: str
    quant: QuantIntelligence
    technical_consensus: TechnicalConsensus | None
    options: OptionsIntelligence | None
    portfolio: PortfolioContext
    market: MarketContext | None = None
    events: EventCalendar | None = None
