"""Delivery layer — Telegram, briefings, onboarding.

Public interface for the delivery module.
"""

from src.delivery.briefing import generate_briefing, format_signals, format_portfolio
from src.delivery.onboarding import (
    auto_classify_portfolio,
    analyze_gaps,
    generate_transition_plan,
    format_onboarding_summary,
)
from src.delivery.telegram_bot import (
    AlertPriority,
    AlertThrottling,
    TelegramFormatter,
    format_execution_result,
    format_gated_alert,
    send_briefing,
    split_message,
)
from src.delivery.reasoning import build_reasoning_prompt, generate_analyst_brief

__all__ = [
    "generate_briefing",
    "format_signals",
    "format_portfolio",
    "auto_classify_portfolio",
    "analyze_gaps",
    "generate_transition_plan",
    "format_onboarding_summary",
    "AlertPriority",
    "AlertThrottling",
    "TelegramFormatter",
    "format_execution_result",
    "format_gated_alert",
    "send_briefing",
    "split_message",
    "build_reasoning_prompt",
    "generate_analyst_brief",
]
