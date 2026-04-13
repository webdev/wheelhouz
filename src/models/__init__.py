"""Shared data models for Wheel Copilot.

All modules import their dataclasses from here.
No module defines its own dataclasses — everything lives in src/models/.
"""

from __future__ import annotations

from src.models.account import AccountRouter, BrokerageAccount
from src.models.analysis import Opportunity, RiskReport, SizedOpportunity, SmartStrike
from src.models.engine import CorePosition, EngineAllocation, RebalanceAction
from src.models.enums import (
    AccountType,
    Conviction,
    Engine,
    EntryDecision,
    PositionAction,
    PositionType,
    Regime,
    SignalType,
    Strategy,
    Urgency,
)
from src.models.execution import GateValidation, LivePriceGate
from src.models.paper import (
    ExecutionRules,
    PaperDashboard,
    PaperPosition,
    PaperSnapshot,
)
from src.models.market import EventCalendar, MarketContext, OptionsChain, PriceHistory
from src.models.onboarding import (
    GapAnalysis,
    PortfolioIntake,
    StockClassification,
    TransitionAction,
    TransitionPlan,
)
from src.models.position import PortfolioState, Position
from src.models.signals import AlphaSignal
from src.models.tax import TaxContext, TaxEngine, TradeTaxImpact, WashSaleTracker

__all__ = [
    # Enums
    "AccountType",
    "Conviction",
    "Engine",
    "EntryDecision",
    "PositionAction",
    "PositionType",
    "Regime",
    "SignalType",
    "Strategy",
    "Urgency",
    # Position & portfolio
    "Position",
    "PortfolioState",
    # Market data
    "MarketContext",
    "PriceHistory",
    "EventCalendar",
    "OptionsChain",
    # Signals
    "AlphaSignal",
    # Analysis
    "SmartStrike",
    "Opportunity",
    "SizedOpportunity",
    "RiskReport",
    # Accounts
    "BrokerageAccount",
    "AccountRouter",
    # Tax
    "WashSaleTracker",
    "TaxContext",
    "TaxEngine",
    "TradeTaxImpact",
    # Engine allocation
    "CorePosition",
    "EngineAllocation",
    "RebalanceAction",
    # Onboarding
    "StockClassification",
    "PortfolioIntake",
    "GapAnalysis",
    "TransitionAction",
    "TransitionPlan",
    # Execution
    "LivePriceGate",
    "GateValidation",
    # Paper trading
    "PaperPosition",
    "PaperSnapshot",
    "PaperDashboard",
    "ExecutionRules",
]
