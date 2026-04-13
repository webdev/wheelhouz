"""Scout aggregator — collects and deduplicates mentions from all sources.

Feeds into Claude analysis layer for sentiment and catalyst classification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal


@dataclass
class ScoutSource:
    """Configuration for a single scout data source."""
    name: str
    source_type: str  # "twitter", "reddit", "discord", "youtube", "news_api", "rss"
    credibility: float  # 0.0 - 1.0
    poll_frequency_minutes: int = 60


@dataclass
class RawMention:
    """A single mention of a ticker from any source."""
    ticker: str
    source: str
    source_type: str
    timestamp: datetime
    text: str
    author: str
    author_followers: int = 0
    engagement: int = 0  # likes, upvotes, retweets
    url: str = ""


@dataclass
class ScoutAnalysis:
    """Claude's analysis of a buzzing ticker."""
    ticker: str
    buzz_score: int  # 0-100
    sentiment: str  # "bullish", "bearish", "neutral"
    catalyst: str
    catalyst_type: str  # "earnings", "upgrade", "flow", "technical", "macro", "social"
    credibility_score: float  # 0-100
    novelty: str  # "new", "developing", "old_news"
    wheel_fit: str  # "excellent", "good", "poor"
    wheel_fit_reasoning: str
    recommended_strategy: str
    urgency: str  # "now", "today", "this_week", "watchlist"


@dataclass
class ScoutOpportunity:
    """A fully qualified scout pick ready for the briefing."""
    ticker: str
    analysis: ScoutAnalysis
    mentions: list[RawMention] = field(default_factory=list)
    composite_score: float = 0.0
    is_qualified: bool = False


# Default source list with credibility weights
DEFAULT_SOURCES: list[ScoutSource] = [
    ScoutSource("unusual_whales", "twitter", 0.80, 15),
    ScoutSource("DeItaone", "twitter", 0.85, 15),
    ScoutSource("OptionsHawk", "twitter", 0.75, 30),
    ScoutSource("r/thetagang", "reddit", 0.60, 120),
    ScoutSource("r/options", "reddit", 0.50, 120),
    ScoutSource("r/wallstreetbets", "reddit", 0.30, 120),
    ScoutSource("benzinga", "news_api", 0.80, 15),
    ScoutSource("marketwatch", "rss", 0.70, 30),
    ScoutSource("thetagang_discord", "discord", 0.60, 60),
    ScoutSource("tastylive", "youtube", 0.70, 240),
]


def aggregate_mentions(
    mentions: list[RawMention],
    lookback_hours: int = 6,
    min_mentions: int = 2,
    min_source_diversity: int = 2,
) -> dict[str, list[RawMention]]:
    """Group mentions by ticker and filter for buzz threshold.

    A ticker qualifies if it has:
    - mentions from 2+ different sources, OR
    - at least 1 mention with 500+ engagement
    """
    cutoff = datetime.utcnow() - timedelta(hours=lookback_hours)
    recent = [m for m in mentions if m.timestamp >= cutoff]

    by_ticker: dict[str, list[RawMention]] = {}
    for m in recent:
        by_ticker.setdefault(m.ticker, []).append(m)

    qualified: dict[str, list[RawMention]] = {}
    for ticker, ticker_mentions in by_ticker.items():
        sources = {m.source for m in ticker_mentions}
        max_engagement = max((m.engagement for m in ticker_mentions), default=0)

        if len(sources) >= min_source_diversity or max_engagement >= 500:
            if len(ticker_mentions) >= min_mentions:
                qualified[ticker] = ticker_mentions

    return qualified


def calculate_buzz_score(mentions: list[RawMention]) -> int:
    """Calculate buzz score (0-100) from mentions."""
    source_diversity = len({m.source for m in mentions})
    total_engagement = sum(m.engagement for m in mentions)

    score = (
        len(mentions) * 10
        + source_diversity * 15
        + min(50, total_engagement // 100)
    )
    return min(100, score)


def calculate_composite_score(
    analysis: ScoutAnalysis,
    annualized_yield: float | None = None,
) -> float:
    """Calculate composite opportunity score for ranking."""
    quant_score = min(100.0, analysis.buzz_score * 0.5 + analysis.credibility_score * 0.5)
    yield_score = (annualized_yield * 200) if annualized_yield else 0.0

    composite = (
        analysis.buzz_score * 0.20
        + analysis.credibility_score * 0.25
        + quant_score * 0.35
        + yield_score * 0.20
    )
    return composite


def format_scout_picks(opportunities: list[ScoutOpportunity]) -> str:
    """Format scout picks for the morning briefing."""
    if not opportunities:
        return "SCOUT PICKS: No qualified opportunities."

    lines = ["SCOUT PICKS"]
    for opp in opportunities[:5]:  # Top 5
        a = opp.analysis
        lines.append(
            f"  {a.ticker}: {a.sentiment.upper()} | "
            f"Buzz {a.buzz_score} | {a.catalyst} | "
            f"Wheel: {a.wheel_fit} | {a.urgency}"
        )
    return "\n".join(lines)
