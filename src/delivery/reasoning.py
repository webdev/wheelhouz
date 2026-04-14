# src/delivery/reasoning.py
"""Claude reasoning engine — synthesize IntelligenceContext into analyst briefs.

Two-tier system:
- Quick scan: mechanical, always available, no API needed
- Analyst brief: Claude-powered reasoning with thesis/dissent
"""
from __future__ import annotations

import structlog

from src.models.intelligence import IntelligenceContext

logger = structlog.get_logger()

MAX_SYMBOLS = 5

SYSTEM_PROMPT = """You are an aggressive options wheel strategy analyst managing a $1M portfolio.
You sell cash-secured puts into fear and covered calls on assignments. Target: 25-40% annualized.

For each symbol, produce a trade analysis:
1. THESIS — why this trade makes sense (or doesn't), in 2-3 sentences
2. CONVICTION — HIGH / MEDIUM / LOW / SKIP with one-line reasoning
3. DISSENT — what argues against this trade (always include this, even for strong trades)
4. TRADE SPEC — if you'd take it: strike, contracts, account, expiration. If SKIP, say why.
5. WHAT CHANGES MY MIND — 1-2 conditions that upgrade or kill this trade

Rules:
- If TradingView consensus disagrees with quant signals, weigh that heavily. The crowd sees something.
- If IV rank is low (<30), premiums are cheap — flag this. Don't sell cheap premium.
- If trend is "downtrend" and the only signals are mean-reversion, be skeptical.
- A single signal is ALWAYS low conviction. Convergence required.
- Be explicit about data gaps ("IV rank unavailable" or "options chain not loaded").
- Use dollar amounts and percentages, not vague language.
- Position reviews: if you wouldn't open this trade today, recommend closing."""


def build_reasoning_prompt(contexts: list[IntelligenceContext]) -> str:
    """Build the user prompt from IntelligenceContext objects.

    Caps at MAX_SYMBOLS to control token budget.
    """
    capped = contexts[:MAX_SYMBOLS]
    sections: list[str] = []

    for ctx in capped:
        lines: list[str] = []
        lines.append(f"=== {ctx.symbol} ===")

        # Quant
        q = ctx.quant
        sig_names = ", ".join(s.signal_type.value for s in q.signals) or "none"
        lines.append(f"QUANT SIGNALS: {q.signal_count} signals [{sig_names}], "
                     f"avg strength {q.avg_strength:.0f}")
        lines.append(f"  RSI: {q.rsi:.1f}" if q.rsi is not None else "  RSI: unavailable")
        lines.append(f"  IV Rank: {q.iv_rank:.0f} (HV-proxy)" if q.iv_rank > 0
                     else "  IV Rank: unavailable")
        lines.append(f"  Trend: {q.trend_direction}")
        if q.price_vs_support:
            support_str = ", ".join(f"{k}: {v:+.1f}%" for k, v in q.price_vs_support.items())
            lines.append(f"  Support distances: {support_str}")

        # TradingView
        tc = ctx.technical_consensus
        if tc:
            lines.append(f"TRADINGVIEW: {tc.overall} "
                         f"({tc.buy_count} buy / {tc.neutral_count} neutral / {tc.sell_count} sell)")
            lines.append(f"  Oscillators: {tc.oscillators} | Moving Averages: {tc.moving_averages}")
        else:
            lines.append("TRADINGVIEW: unavailable")

        # Options
        opt = ctx.options
        if opt and opt.chain_available:
            lines.append(f"OPTIONS: chain loaded, best put at ${opt.best_strike.strike} "
                         f"(${opt.best_strike.premium} mid, {opt.annualized_yield:.0%} ann)")
            lines.append(f"  Bid-ask spread: {opt.bid_ask_spread_pct:.1f}%")
        else:
            lines.append("OPTIONS: chain not loaded (premiums estimated)")

        # Portfolio
        p = ctx.portfolio
        if p.existing_positions:
            lines.append(f"PORTFOLIO: {len(p.existing_positions)} existing position(s), "
                         f"{p.existing_exposure_pct:.1%} exposure")
        else:
            lines.append(f"PORTFOLIO: no existing exposure, "
                         f"${p.available_capital:,.0f} available")
        if p.wash_sale_blocked:
            lines.append("  BLOCKED: wash sale window active")
        if p.earnings_conflict:
            lines.append("  WARNING: earnings within expiration window")

        # Market
        if ctx.market:
            m = ctx.market
            lines.append(f"MARKET: ${m.price} | 1d: {m.price_change_1d:+.1f}% | "
                         f"5d: {m.price_change_5d:+.1f}% | vs 52wH: {m.price_vs_52w_high:+.1f}%")

        sections.append("\n".join(lines))

    return "\n\n".join(sections)


def _build_client() -> tuple[object, str] | None:
    """Build the appropriate Anthropic client based on available credentials.

    Checks in order:
    1. AWS_BEARER_TOKEN_BEDROCK → AnthropicBedrock (bearer token auth)
    2. AWS credentials (profile/keys) → AnthropicBedrock (standard AWS auth)
    3. ANTHROPIC_API_KEY → AsyncAnthropic (direct API)

    Returns (client, model_id) or None if no credentials found.
    """
    import os

    try:
        import anthropic
    except ImportError:
        logger.info("anthropic_not_installed")
        return None

    # Option 1: AWS Bedrock with bearer token
    bearer_token = os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
    if bearer_token:
        region = os.environ.get("AWS_REGION", "us-west-2")
        client = anthropic.AsyncAnthropicBedrock(
            aws_region=region,
            aws_session_token=bearer_token,
            aws_access_key="",
            aws_secret_key="",
        )
        model = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6-v1")
        logger.info("using_bedrock", region=region, model=model)
        return client, model

    # Option 2: Standard AWS credentials for Bedrock
    if os.environ.get("AWS_PROFILE") or os.environ.get("AWS_ACCESS_KEY_ID"):
        region = os.environ.get("AWS_REGION", "us-west-2")
        client = anthropic.AsyncAnthropicBedrock(aws_region=region)
        model = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6-v1")
        logger.info("using_bedrock", region=region, model=model)
        return client, model

    # Option 3: Direct Anthropic API
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        client = anthropic.AsyncAnthropic(api_key=api_key)
        model = "claude-sonnet-4-20250514"
        logger.info("using_anthropic_api")
        return client, model

    logger.info("no_claude_credentials")
    return None


async def generate_analyst_brief(
    contexts: list[IntelligenceContext],
    regime_summary: str = "",
) -> str | None:
    """Call Claude API to generate reasoned analyst brief.

    Supports AWS Bedrock (bearer token or standard auth) and direct Anthropic API.
    Returns None if no credentials are available or the call fails.
    """
    result = _build_client()
    if result is None:
        return None

    client, model = result

    user_prompt = build_reasoning_prompt(contexts)
    if regime_summary:
        user_prompt = f"REGIME: {regime_summary}\n\n{user_prompt}"

    try:
        response = await client.messages.create(
            model=model,
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return response.content[0].text
    except Exception as e:
        logger.warning("claude_reasoning_failed", error=str(e))
        return None
