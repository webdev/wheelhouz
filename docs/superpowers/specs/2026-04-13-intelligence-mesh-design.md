# Intelligence Mesh — Multi-Source Reasoning for Wheel Copilot

## Problem

The briefing system produces mechanical recommendations based solely on quantitative signals with incomplete data. IV rank is disconnected, premiums are estimated, there's no external technical consensus, no portfolio awareness, and no reasoning explaining *why* a trade is recommended. The result: marginal trades like PLTR ($125P, 17% annualized, confirmed downtrend) surface as MEDIUM conviction with no dissent.

The user can do options trading themselves. The system's value is as an **aggregation of wisdom** — synthesizing quant signals, technical consensus, financial data, and portfolio context into reasoned recommendations with transparent thinking.

## Solution

An intelligence mesh that collects inputs from multiple independent sources, feeds them into a unified data model (`IntelligenceContext`), and hands that context to Claude for reasoned synthesis. The system produces analyst-quality briefs with thesis, conviction, dissent, and "what changes my mind" for both new trades and existing positions.

## Architecture

### IntelligenceContext — Unified Input Model

**Location:** `src/models/intelligence.py` — all dataclasses below live here (`IntelligenceContext`, `QuantIntelligence`, `TechnicalConsensus`, `OptionsIntelligence`, `PortfolioContext`), following the project rule that shared models go in `src/models/`.

One `IntelligenceContext` per symbol, collected during each analysis cycle. Every intelligence source fills its slice. Missing sources are `None` — Claude acknowledges gaps rather than hallucinating.

```
IntelligenceContext
  symbol: str

  quant: QuantIntelligence
    signals: list[AlphaSignal]        # existing 13 alpha signals
    signal_count: int
    avg_strength: float
    iv_rank: float                    # HV-proxy, always calculated
    iv_percentile: float
    rsi: float
    price_vs_support: dict[str, float]  # {"200 SMA": 4.6, "50 SMA": 0.5, ...}
    trend_direction: str              # "uptrend" / "downtrend" / "range"

  technical_consensus: TechnicalConsensus | None
    source: "tradingview"
    overall: str                      # STRONG_BUY / BUY / NEUTRAL / SELL / STRONG_SELL
    oscillators: str
    moving_averages: str
    buy_count: int
    neutral_count: int
    sell_count: int
    raw_indicators: dict[str, float]  # RSI, MACD, Stoch values

  options: OptionsIntelligence | None
    best_strike: SmartStrike | None
    iv_rank: float
    premium_yield: float              # real, from chain
    annualized_yield: float
    bid_ask_spread_pct: float
    chain_available: bool

  portfolio: PortfolioContext
    existing_exposure_pct: float
    existing_positions: list[Position]   # open positions in this symbol
    account_recommendation: str       # "Roth IRA" / "Taxable" / etc.
    wash_sale_blocked: bool
    earnings_conflict: bool
    available_capital: Decimal

  market: MarketContext               # existing model, now with real IV rank
  events: EventCalendar               # existing model
```

### Component 1: TradingView Intelligence Layer

**Library:** `tradingview-ta` (pip install, no API key needed)

**Function:** `fetch_tradingview_consensus(symbol: str) -> TechnicalConsensus`

Fetches the technical analysis summary that millions of traders see on TradingView. One HTTP call per symbol, fast, no auth.

**Role in the system:**
- Independent second opinion on quant signals. If quant says "oversold, sell puts" but TradingView consensus is SELL (bearish), that's dissent.
- Trend context — the biggest gap today. Moving average summary flags "downtrend" vs "dip in an uptrend." Critical for put sellers.
- Indicator cross-validation against our own RSI/MACD calculations.

**Design rule:** TradingView consensus can veto or boost conviction, but never initiates a trade on its own. Our 13 quant signals remain the trigger; TradingView confirms or dissents.

**Operational risk:** `tradingview-ta` scrapes an unofficial TradingView endpoint. At 5 daily cycles x 15 symbols = 75 calls/day, rate limiting or blocking is possible. Mitigations:
- Cache results per symbol with a 1-hour TTL (technical consensus doesn't change minute-to-minute).
- Treat HTTP 403/429 as graceful degradation: set `technical_consensus = None` for that symbol and note "TradingView unavailable" in the briefing.
- This reduces actual HTTP calls to ~15/hour worst case.

### Component 2: Data Fixes (parallel workstream)

Three disconnected pieces that need wiring:

**Fix 1 — IV Rank (logic fix, not just wiring)**

`src/data/market.py:206` skips `calculate_iv_rank()` when `current_iv=0`. But `calculate_iv_rank()` uses `current_iv` as the value to rank — calling it with 0 produces a rank of 0, which is meaningless. The fix requires two changes:

1. Modify `calculate_iv_rank()` to fall back to `hv_30d` (the 30-day rolling realized vol it already computes) as the current IV proxy when `current_iv=0`. Rank this HV value against its own 252-day history.
2. Always call `calculate_iv_rank()` in `fetch_market_context()`, removing the `if current_iv > 0` guard.
3. Label the result as "HV-proxy" in the briefing output so it's clear this is not true implied vol.

Not true IV rank, but a meaningful measure of "is volatility high or low for this name right now." Good enough for conviction gating until E*Trade production provides real IV from option chains.

**Fix 2 — Real options chain from yfinance (model change required)**

yfinance provides `ticker.options` (expiration dates) and `ticker.option_chain(date)` (strikes with bid, ask, volume, OI, implied vol).

The current `OptionsChain` model in `src/models/market.py` only holds scalar summaries (`atm_iv`, `historical_skew_25d`, `iv_by_expiry`). It has no field for per-contract data. This fix requires:

1. Add a new `OptionContract` dataclass to `src/models/market.py`:
   ```
   OptionContract: strike (Decimal), expiration (date), option_type (str),
                   bid (Decimal), ask (Decimal), mid (Decimal),
                   volume (int), open_interest (int),
                   implied_vol (float), delta (float)
   ```
2. Extend `OptionsChain` with `puts: list[OptionContract]` and `calls: list[OptionContract]`.
3. New function `fetch_options_chain(symbol: str) -> OptionsChain` in `src/data/market.py` (alongside existing `fetch_market_context` and `fetch_price_history`) that populates real strikes from yfinance.
4. `find_smart_strikes()` in `strikes.py` should prefer real chain data (bid/ask/delta) over the estimated premium formula, falling back to estimation only when chain is unavailable.

This also unblocks `detect_skew_blowout` and `detect_term_inversion` signals which depend on chain fields that are never populated today.

**Fix 3 — Portfolio awareness (with position model conversion)**

Pull positions from Alpaca (paper) or E*Trade (live) at the start of each briefing cycle. Build a `PortfolioContext` per symbol: current exposure, P&L on existing positions, available capital.

`AlpacaPosition` (in `src/execution/alpaca_client.py`) and the shared `Position` model (in `src/models/position.py`) are structurally different — `AlpacaPosition` lacks greeks, `days_to_expiry`, `profit_pct`, etc. This fix includes:

1. A conversion function `alpaca_position_to_position(ap: AlpacaPosition) -> Position` that populates derived fields. Greeks come from the yfinance option chain data (Fix 2). Days to expiry parsed from the OCC symbol.
2. Equivalent conversion for E*Trade positions via the existing `broker.py` fetch.
3. Group positions by underlying symbol to build the per-symbol `PortfolioContext`.

### Component 3: Claude Reasoning Engine

**Two-tier output:**

1. **Quick scan** (always, no API needed): The mechanical briefing — regime, signals, watchlist table. Free, fast, works offline.
2. **Analyst brief** (Claude-powered, opt-in): Reasoned analysis with thesis/dissent per symbol. ~2K input tokens + ~800 output tokens per symbol, $0.15-0.30 per briefing for 3-5 symbols. See Dependencies section for full cost model.

**Per-symbol prompt structure:**

For each symbol with fired signals (or open positions), serialize the `IntelligenceContext` and ask Claude to produce:

1. **THESIS** — why this trade makes sense (or doesn't)
2. **CONVICTION** — HIGH / MEDIUM / LOW / SKIP with reasoning
3. **DISSENT** — what argues against this trade
4. **TRADE SPEC** — if recommended: strike, size, account, expiration
5. **WHAT CHANGES MY MIND** — conditions that would upgrade or kill this trade

**Design rules:**
- Claude can disagree with quant signals. If signals say MEDIUM but TradingView says STRONG_SELL and IV rank is low, Claude can downgrade to SKIP with reasoning.
- Claude cannot override hard risk rules. Loss stops, concentration limits, wash sale blocks, earnings conflicts are code-enforced.
- Graceful fallback. No API key or no internet → system produces the mechanical briefing only.

**System prompt principles:**
- Aggressive wheel philosophy (sell into fear, every dollar working)
- Explicit about data gaps ("IV rank unavailable, discounting premium assessment")
- Must include dissent even on strong recommendations
- Dollar amounts and percentages, not vague language

### Component 4: Position Intelligence

**The gap:** The briefing only answers "what should I open?" It never says "what should I close?"

**The fix:** Every briefing cycle runs two passes:

1. **Entry scan** — quant signals + TradingView + Claude on watchlist symbols
2. **Position scan** — for every open position, build an `IntelligenceContext` and evaluate

**Position scan recommendations:**

| Action | Trigger |
|--------|---------|
| CLOSE NOW | Loss stop hit, earnings conflict, intelligence consensus flipped bearish |
| TAKE PROFIT | Hit profit target, or favorable conditions that justified entry have faded |
| WATCH CLOSELY | Not at trigger yet, but something changed (TV flipped, IV dropped) |
| HOLD | Everything healthy, thesis intact |

**Key principle:** The same intelligence that decides entries continuously re-evaluates open positions. If the system wouldn't recommend opening the trade today, it should tell you to consider closing it.

**Example position review output:**

```
! PLTR short $125P — CLOSE RECOMMENDED

You sold this put when RSI was oversold and sector rotation fired.
Since then: TradingView consensus is SELL, trend confirmed down,
no stabilization signals. The thesis (mean reversion) is not
playing out.

Current P&L: -$85 (-47% of premium received)
Days to expiry: 22

Recommendation: Buy to close. Take the small loss now rather
than risk assignment on a name in a confirmed downtrend.
Redeploy the $12,500 into a higher-conviction setup.
```

## Briefing Output Structure

The full briefing combines mechanical and reasoned sections:

```
============================================================
  WHEEL COPILOT — MORNING BRIEFING
  Monday, April 13, 2026
============================================================

━━ REGIME ━━
  [HOLD] VIX 19.1 | SPY +0.98% | Deploy up to 70%

━━ POSITION REVIEW ━━         <-- NEW: manage what you own
  (per open position: HOLD / WATCH / CLOSE with reasoning)

━━ ACTION PLAN ━━              <-- ENHANCED: Claude-reasoned
  (per opportunity: thesis, conviction, dissent, trade spec)

━━ SIGNAL FLASH ━━             (existing, kept for transparency)
━━ WATCHLIST ━━                (existing)
━━ EARNINGS WATCH ━━           (existing)
━━ TAX ALERTS ━━               (existing)
━━ MACRO ━━                    (existing)
```

Position review comes before action plan because managing existing risk is more important than finding new trades.

**Sections removed from current briefing:** DIP OPPORTUNITIES, NEAR SUPPORT, and OVERSOLD are removed as standalone sections. Their information is subsumed by the enhanced ACTION PLAN — Claude's reasoning naturally covers "near 50 SMA support" and "RSI oversold" as part of the thesis/dissent for each recommendation. Keeping them as separate sections would be redundant.

## Pipeline Flow

```
Analysis Cycle
  1. Fetch VIX/SPY → classify regime
  2. Load watchlist + fetch per-symbol data (yfinance)
  3. Fetch TradingView consensus per symbol        [NEW]
  4. Fetch real options chain per symbol            [NEW]
  5. Load portfolio positions                       [NEW]
  6. Detect quant signals (existing 13 detectors)
  7. Build IntelligenceContext per symbol           [NEW]
  8. Position scan: evaluate open positions         [NEW]
  9. Entry scan: evaluate new opportunities
  10. Claude reasoning: synthesize all context      [NEW]
  11. Risk gates (concentration, wash sale, etc.)
  12. Format briefing (mechanical + analyst brief)
  13. Deliver (stdout, Telegram)
```

## Dependencies

| Dependency | Purpose | Auth |
|------------|---------|------|
| `tradingview-ta` | Technical consensus | None (free) |
| `yfinance` | Options chains, price data, IV | None (existing) |
| `anthropic` | Claude reasoning engine | ANTHROPIC_API_KEY |
| `alpaca-py` | Portfolio positions (paper) | ALPACA keys (existing) |
| `pyetrade` | Portfolio positions (live) | E*Trade keys (existing) |

Claude API is the only new cost. Estimated at Sonnet 4 pricing ($3/M input, $15/M output): ~2K input tokens per symbol, ~800 output tokens per symbol, 3-5 symbols per briefing = $0.15-0.30 per briefing. At 5x daily = $0.75-1.50/day.

**Cost controls:** Use a single batched API call (all symbols in one prompt) rather than per-symbol calls to reduce latency and overhead. Budget: ~800 output tokens per symbol, max 5 symbols per analyst brief, so set `max_tokens=4000` on the API call. Symbols beyond 5 get mechanical-only analysis. This keeps daily cost under $2.

## What This Does NOT Include

- Scout social layer (Reddit, Twitter, Discord) — future phase, once this mesh proves the value of multi-source synthesis
- Automated execution from Claude recommendations — Claude advises, user decides
- Real-time streaming — briefings run on the existing 5x daily schedule
- Backtesting Claude's reasoning — tracked via the existing learning loop's attribution system

## Success Criteria

1. Briefing includes TradingView consensus per symbol with agreement/dissent vs quant signals
2. IV rank is calculated and displayed for every watchlist symbol (no more "N/A")
3. Options premiums are real quotes from yfinance chains, not estimated
4. Briefing shows existing positions with hold/watch/close recommendations
5. Claude analyst brief explains thesis, conviction, dissent for each recommendation
6. System gracefully degrades: no API key → mechanical briefing only
7. PLTR-type scenario correctly identified: quant says BUY, trend says SELL → SKIP with explanation
