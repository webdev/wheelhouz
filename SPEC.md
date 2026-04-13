# Wheel Copilot — Phase 1 Architecture Spec

## Overview

A daily morning briefing agent that analyzes your ~25-position wheel strategy portfolio against live market data, generates concrete action recommendations, and delivers them as a push notification before market open.

**Goal:** Replace the 30-60 minutes of manual morning review with a 2-minute read that tells you exactly what to do today and why.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    CRON TRIGGER                          │
│              Weekdays @ 8:00 AM ET                       │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│                 DATA COLLECTION LAYER                    │
│                                                         │
│  ┌──────────────┐ ┌──────────────┐ ┌────────────────┐  │
│  │  Broker API   │ │  Market Data │ │  Events/News   │  │
│  │  (IBKR/Schwab)│ │  (yfinance/  │ │  (Earnings,    │  │
│  │              │ │   CBOE)      │ │   Fed, Macro)  │  │
│  └──────┬───────┘ └──────┬───────┘ └───────┬────────┘  │
│         │                │                  │           │
│         ▼                ▼                  ▼           │
│  ┌─────────────────────────────────────────────────┐    │
│  │            Portfolio State Object               │    │
│  │  (positions, greeks, IV, prices, events)        │    │
│  └──────────────────────┬──────────────────────────┘    │
└─────────────────────────┼───────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│                  ANALYSIS ENGINE                         │
│                                                         │
│  ┌──────────────┐ ┌──────────────┐ ┌────────────────┐  │
│  │  Position     │ │  Opportunity │ │  Risk/          │  │
│  │  Scanner      │ │  Finder      │ │  Concentration  │  │
│  │              │ │              │ │  Monitor        │  │
│  └──────┬───────┘ └──────┬───────┘ └───────┬────────┘  │
│         │                │                  │           │
│         ▼                ▼                  ▼           │
│  ┌─────────────────────────────────────────────────┐    │
│  │         Claude API Reasoning Layer              │    │
│  │  (synthesize data → actionable briefing)        │    │
│  └──────────────────────┬──────────────────────────┘    │
└─────────────────────────┼───────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│                  DELIVERY LAYER                          │
│                                                         │
│  ┌──────────────┐ ┌──────────────┐ ┌────────────────┐  │
│  │  Telegram Bot │ │  Trade Log   │ │  Performance   │  │
│  │  (push msg)   │ │  (Postgres)  │ │  Tracker       │  │
│  └──────────────┘ └──────────────┘ └────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

---

## Day 1: Portfolio Onboarding Module

Before the system runs a single daily briefing, it needs to understand what you 
already own, why you own it, and how to transition to the three-engine model 
without creating unnecessary tax events or closing positions at bad times.

This runs ONCE at setup, then the advisor layer maintains it going forward.

### Step 1: Portfolio Intake & Classification

```python
@dataclass
class PortfolioIntake:
    """
    Pull everything from E*Trade and classify every position.
    The system can auto-classify most positions, but needs human 
    input on conviction level and engine assignment for stocks.
    """
    
    # Auto-classifiable (no human input needed)
    short_puts: list[Position]          # → Engine 2 automatically
    short_calls: list[Position]         # → Engine 2 automatically
    cash: float                         # → Engine 3 automatically
    
    # Needs human classification
    stock_positions: list[StockClassification]  # → Engine 1 or 2?
    
    # Special handling
    rsu_espp_positions: list[Position]  # → concentration plan
    
    # Tax context (critical for transition planning)
    positions_with_gains: list[TaxContext]
    positions_with_losses: list[TaxContext]
    wash_sale_risk_tickers: list[str]   # tickers closed at a loss in last 30 days


@dataclass
class StockClassification:
    """
    For each stock position, the system needs ONE human input:
    Is this a long-term compounder (Engine 1) or a wheel candidate (Engine 2)?
    
    This determines:
    - Engine 1: keep 60-70% uncovered, sell calls on 30-40% at far OTM
    - Engine 2: sell calls on 100% at standard delta, welcome assignment away
    """
    symbol: str
    shares: int
    cost_basis: float
    current_price: float
    unrealized_pnl: float
    holding_period_days: int
    is_ltcg: bool               # held >1 year
    
    # Human input (via Telegram onboarding flow)
    engine: str | None = None    # "engine1" or "engine2" — set by user
    conviction: str | None = None  # "high", "medium", "low"
    
    # System-suggested classification (user confirms or overrides)
    suggested_engine: str | None = None
    suggestion_reason: str | None = None


@dataclass
class TaxContext:
    """Tax information for each position — critical for transition planning."""
    symbol: str
    cost_basis_per_share: float
    current_price: float
    unrealized_gain: float
    unrealized_gain_pct: float
    purchase_date: date
    holding_period_days: int
    is_ltcg: bool
    
    # Tax impact of selling NOW
    estimated_tax_if_sold: float           # at current rates
    estimated_tax_if_waited_for_ltcg: float  # if short-term, what if you wait?
    tax_savings_by_waiting: float
    days_until_ltcg: int | None            # None if already LTCG


def auto_classify_portfolio(positions: list[Position]) -> PortfolioIntake:
    """
    Auto-classify what we can. Flag what needs human input.
    """
    intake = PortfolioIntake(
        short_puts=[], short_calls=[], cash=0,
        stock_positions=[], rsu_espp_positions=[],
        positions_with_gains=[], positions_with_losses=[],
        wash_sale_risk_tickers=[]
    )
    
    for pos in positions:
        # Options auto-classify to Engine 2
        if pos.position_type == "short_put":
            intake.short_puts.append(pos)
            continue
        if pos.position_type == "short_call":
            intake.short_calls.append(pos)
            continue
        if pos.position_type == "cash":
            intake.cash += pos.market_value
            continue
        
        # Stock positions need human input, but we can suggest
        stock = StockClassification(
            symbol=pos.symbol,
            shares=pos.quantity,
            cost_basis=pos.cost_basis,
            current_price=pos.current_price,
            unrealized_pnl=pos.unrealized_pnl,
            holding_period_days=pos.holding_period_days,
            is_ltcg=pos.holding_period_days > 365,
        )
        
        # Heuristic suggestions
        if pos.symbol == "ADBE":
            stock.suggested_engine = "engine2"
            stock.suggestion_reason = "RSU/ESPP concentration — sell down over time"
            intake.rsu_espp_positions.append(pos)
        elif pos.holding_period_days > 365 and pos.unrealized_pnl > 0:
            stock.suggested_engine = "engine1"
            stock.suggestion_reason = "LTCG with gains — likely a compounder you believe in"
        elif pos.quantity == 100 and pos.holding_period_days < 60:
            stock.suggested_engine = "engine2"
            stock.suggestion_reason = "Round lot, recently acquired — likely from put assignment"
        else:
            stock.suggested_engine = "engine1"
            stock.suggestion_reason = "Default to Engine 1 — override if this is a wheel position"
        
        intake.stock_positions.append(stock)
        
        # Tax context
        tax = TaxContext(
            symbol=pos.symbol,
            cost_basis_per_share=pos.cost_basis / pos.quantity if pos.quantity else 0,
            current_price=pos.current_price,
            unrealized_gain=pos.unrealized_pnl,
            unrealized_gain_pct=(pos.unrealized_pnl / pos.cost_basis * 100) if pos.cost_basis else 0,
            purchase_date=pos.purchase_date,
            holding_period_days=pos.holding_period_days,
            is_ltcg=pos.holding_period_days > 365,
            estimated_tax_if_sold=estimate_tax(pos.unrealized_pnl, pos.holding_period_days > 365),
            estimated_tax_if_waited_for_ltcg=estimate_tax(pos.unrealized_pnl, True),
            tax_savings_by_waiting=0,  # computed below
            days_until_ltcg=(365 - pos.holding_period_days) if pos.holding_period_days < 365 else None,
        )
        if tax.days_until_ltcg and tax.days_until_ltcg > 0:
            tax.tax_savings_by_waiting = tax.estimated_tax_if_sold - tax.estimated_tax_if_waited_for_ltcg
        
        if pos.unrealized_pnl > 0:
            intake.positions_with_gains.append(tax)
        else:
            intake.positions_with_losses.append(tax)
    
    return intake


def estimate_tax(gain: float, is_ltcg: bool) -> float:
    """Rough tax estimate. Assumes 37% marginal for STCG, 20% for LTCG."""
    if gain <= 0:
        return 0
    return gain * (0.20 if is_ltcg else 0.37)
```

### Step 2: Gap Analysis

```python
@dataclass
class GapAnalysis:
    """
    Compare current portfolio to target three-engine allocation.
    Identify everything that's wrong and prioritize fixing it.
    """
    
    # Current vs target allocation
    engine1_current_pct: float
    engine1_target_pct: float = 0.45
    engine1_gap: float = 0.0          # positive = need more, negative = too much
    
    engine2_current_pct: float
    engine2_target_pct: float = 0.45
    engine2_gap: float = 0.0
    
    engine3_current_pct: float
    engine3_target_pct: float = 0.10
    engine3_gap: float = 0.0
    
    # Issues found (sorted by urgency)
    critical_issues: list[str]        # fix this week
    important_issues: list[str]       # fix this month
    optimization_issues: list[str]    # fix over 1-3 months
    
    # Position-level issues
    stranded_profits: list[dict]      # positions past 80% profit just sitting there
    earnings_conflicts: list[dict]    # open options with earnings before expiry
    concentration_violations: list[dict]  # any name >10%, any sector >35%
    tax_traps: list[dict]             # STCG positions approaching LTCG threshold
    missing_hedges: list[dict]        # no tail protection
    uncovered_stock: list[dict]       # stock without calls (Engine 2 positions)
    
    # Greeks assessment
    current_delta: float
    target_delta_range: tuple[float, float]
    current_theta: float
    estimated_target_theta: float     # what theta SHOULD be at full deployment


def analyze_gaps(
    intake: PortfolioIntake,
    nlv: float
) -> GapAnalysis:
    """
    Comprehensive gap analysis of the current portfolio.
    """
    gap = GapAnalysis(
        engine1_current_pct=0, engine2_current_pct=0, engine3_current_pct=0,
        critical_issues=[], important_issues=[], optimization_issues=[],
        stranded_profits=[], earnings_conflicts=[], concentration_violations=[],
        tax_traps=[], missing_hedges=[], uncovered_stock=[],
        current_delta=0, target_delta_range=(200, 500),
        current_theta=0, estimated_target_theta=0
    )
    
    # === CRITICAL (fix this week) ===
    
    # Stranded profits — money sitting on the table
    for pos in intake.short_puts + intake.short_calls:
        if pos.profit_pct >= 0.80:
            gap.critical_issues.append(
                f"🔥 {pos.symbol} {pos.strike}{pos.option_type[0]} at {pos.profit_pct:.0%} profit. "
                f"Close NOW. Collecting pennies while risking dollars."
            )
            gap.stranded_profits.append({"position": pos, "profit_pct": pos.profit_pct})
        elif pos.profit_pct >= 0.50 and pos.days_to_expiry > 21:
            gap.critical_issues.append(
                f"💰 {pos.symbol} {pos.strike}{pos.option_type[0]} at {pos.profit_pct:.0%} profit, "
                f"{pos.days_to_expiry} DTE. Close early and redeploy capital."
            )
            gap.stranded_profits.append({"position": pos, "profit_pct": pos.profit_pct})
    
    # Earnings conflicts
    for pos in intake.short_puts + intake.short_calls:
        cal = get_event_calendar(pos.symbol)
        if cal.next_earnings and cal.next_earnings <= pos.expiration:
            days_to_er = (cal.next_earnings - date.today()).days
            gap.critical_issues.append(
                f"⚠️ {pos.symbol} {pos.strike}{pos.option_type[0]} has earnings in {days_to_er} days "
                f"before {pos.expiration} expiry. Close or accept earnings risk."
            )
            gap.earnings_conflicts.append({"position": pos, "earnings_date": cal.next_earnings})
    
    # Loss stop violations already in effect
    for pos in intake.short_puts + intake.short_calls:
        loss_multiple = pos.current_price / pos.entry_price if pos.entry_price else 0
        if loss_multiple >= 2.0:
            gap.critical_issues.append(
                f"🛑 {pos.symbol} {pos.strike}{pos.option_type[0]} at {loss_multiple:.1f}x entry premium. "
                f"LOSS STOP ALREADY BREACHED. Close immediately."
            )
    
    # === IMPORTANT (fix this month) ===
    
    # Concentration violations
    symbol_exposure = {}
    for pos in intake.stock_positions:
        value = pos.shares * pos.current_price
        symbol_exposure[pos.symbol] = symbol_exposure.get(pos.symbol, 0) + value
    
    for sym, value in symbol_exposure.items():
        pct = value / nlv
        if pct > 0.10:
            gap.important_issues.append(
                f"📊 {sym} concentration at {pct:.0%} of NLV (${value:,.0f}). "
                f"Target <10%. Begin selling down."
            )
            gap.concentration_violations.append({"symbol": sym, "pct": pct, "value": value})
    
    # Engine 2 stocks without covered calls
    for stock in intake.stock_positions:
        if stock.suggested_engine == "engine2" or stock.engine == "engine2":
            has_call = any(
                c.symbol == stock.symbol 
                for c in intake.short_calls
            )
            if not has_call and stock.shares >= 100:
                gap.important_issues.append(
                    f"📝 {stock.symbol}: {stock.shares} shares in Engine 2 with NO covered calls. "
                    f"Sell calls immediately to start collecting income."
                )
                gap.uncovered_stock.append({"symbol": stock.symbol, "shares": stock.shares})
    
    # No tail hedge
    has_spy_put = any(
        p.symbol in ("SPY", "SPX", "QQQ") and p.position_type == "long_put" 
        for p in intake.short_puts  # would actually be in a separate long options list
    )
    if not has_spy_put:
        gap.important_issues.append(
            "🛡️ No portfolio tail hedge. Buy SPY puts (5% OTM, 30-45 DTE) "
            "once the system is running. Budget: 1-2% of NLV annually."
        )
        gap.missing_hedges.append({"type": "tail_hedge", "estimated_cost_pct": 0.015})
    
    # === OPTIMIZATION (fix over 1-3 months) ===
    
    # Tax optimization opportunities
    for tax in intake.positions_with_gains:
        if not tax.is_ltcg and tax.days_until_ltcg and tax.days_until_ltcg < 90:
            gap.optimization_issues.append(
                f"⏰ {tax.symbol}: ${tax.unrealized_gain:,.0f} unrealized STCG, "
                f"becomes LTCG in {tax.days_until_ltcg} days. "
                f"Wait to save ${tax.tax_savings_by_waiting:,.0f} in taxes."
            )
            gap.tax_traps.append(tax)
    
    # Tax-loss harvesting opportunities
    for tax in intake.positions_with_losses:
        if tax.unrealized_gain < -2000:
            gap.optimization_issues.append(
                f"📉 {tax.symbol}: ${abs(tax.unrealized_gain):,.0f} unrealized loss. "
                f"Consider tax-loss harvest — sell, wait 31 days (wash sale), "
                f"re-enter via put sale."
            )
    
    return gap
```

### Step 3: Transition Plan

```python
@dataclass
class TransitionPlan:
    """
    Phased migration from current portfolio to three-engine model.
    NEVER forces a tax-inefficient sale. NEVER closes a working position 
    at a bad time. Transitions organically as positions expire and new 
    trades deploy through the system.
    """
    
    # Phase 1: Immediate (this week) — no-brainer actions
    immediate_actions: list[TransitionAction]
    
    # Phase 2: Short-term (next 2-4 weeks) — let positions expire naturally
    short_term_actions: list[TransitionAction]
    
    # Phase 3: Medium-term (1-3 months) — gradual rebalancing
    medium_term_actions: list[TransitionAction]
    
    # Projected state after full transition
    projected_engine1_pct: float
    projected_engine2_pct: float
    projected_engine3_pct: float
    projected_daily_theta: float
    estimated_transition_weeks: int


@dataclass
class TransitionAction:
    """A single action in the transition plan."""
    urgency: str             # "immediate", "short_term", "medium_term"
    action: str              # "close", "hold", "sell_calls", "reclassify", "buy_hedge"
    symbol: str
    description: str
    tax_impact: float        # estimated tax cost of this action
    opportunity_cost: float  # estimated cost of NOT doing this action
    rationale: str


def generate_transition_plan(
    intake: PortfolioIntake,
    gap: GapAnalysis,
    nlv: float
) -> TransitionPlan:
    """
    Create the phased transition plan.
    
    Core principles:
    1. Don't sell winners near LTCG threshold
    2. Don't force-close positions at a loss unless loss stop is breached
    3. Let existing short options expire naturally when profitable
    4. Route ALL new trades through the signal system from day 1
    5. Rebalance through new cash flows, not liquidation
    """
    
    plan = TransitionPlan(
        immediate_actions=[], short_term_actions=[], medium_term_actions=[],
        projected_engine1_pct=0.45, projected_engine2_pct=0.45,
        projected_engine3_pct=0.10, projected_daily_theta=0,
        estimated_transition_weeks=8
    )
    
    # === IMMEDIATE: Close stranded profits ===
    for sp in gap.stranded_profits:
        pos = sp["position"]
        plan.immediate_actions.append(TransitionAction(
            urgency="immediate",
            action="close",
            symbol=pos.symbol,
            description=f"Close {pos.symbol} {pos.strike}{pos.option_type[0]} "
                        f"at {sp['profit_pct']:.0%} profit. Frees ${pos.capital_at_risk:,.0f}.",
            tax_impact=estimate_tax(pos.current_profit, False),  # options are always STCG
            opportunity_cost=pos.current_profit * 0.3,  # risk of giving back 30% of profit
            rationale="Dead theta. Capital earning nothing. Redeploy into fresh signal."
        ))
    
    # === IMMEDIATE: Close loss stop violations ===
    for pos in intake.short_puts + intake.short_calls:
        loss_multiple = pos.current_price / pos.entry_price if pos.entry_price else 0
        if loss_multiple >= 2.0:
            plan.immediate_actions.append(TransitionAction(
                urgency="immediate",
                action="close",
                symbol=pos.symbol,
                description=f"Close {pos.symbol} {pos.strike}{pos.option_type[0]} — "
                            f"loss stop breached at {loss_multiple:.1f}x entry.",
                tax_impact=0,  # loss = no tax, actually a tax benefit
                opportunity_cost=0,
                rationale="Loss management rule. Close the bleed, preserve capital."
            ))
    
    # === IMMEDIATE: Resolve earnings conflicts ===
    for ec in gap.earnings_conflicts:
        pos = ec["position"]
        if pos.profit_pct > 0.30:
            plan.immediate_actions.append(TransitionAction(
                urgency="immediate",
                action="close",
                symbol=pos.symbol,
                description=f"Close {pos.symbol} {pos.strike}{pos.option_type[0]} "
                            f"at {pos.profit_pct:.0%} profit BEFORE earnings "
                            f"on {ec['earnings_date']}.",
                tax_impact=estimate_tax(pos.current_profit, False),
                opportunity_cost=0,
                rationale="Never hold short options through earnings on day 1."
            ))
    
    # === IMMEDIATE: Sell covered calls on Engine 2 stocks ===
    for us in gap.uncovered_stock:
        plan.immediate_actions.append(TransitionAction(
            urgency="immediate",
            action="sell_calls",
            symbol=us["symbol"],
            description=f"Sell covered calls against {us['shares']} shares of {us['symbol']}. "
                        f"Use 0.25-0.30 delta, 30 DTE. Start generating income immediately.",
            tax_impact=0,
            opportunity_cost=us["shares"] * 2,  # rough estimate of missed daily theta
            rationale="Stock sitting idle in Engine 2. Put it to work."
        ))
    
    # === SHORT-TERM: Let existing options expire naturally ===
    for pos in intake.short_puts + intake.short_calls:
        if pos not in [sp["position"] for sp in gap.stranded_profits]:
            if pos.profit_pct < 0.50 and pos.days_to_expiry <= 21:
                plan.short_term_actions.append(TransitionAction(
                    urgency="short_term",
                    action="hold",
                    symbol=pos.symbol,
                    description=f"Let {pos.symbol} {pos.strike}{pos.option_type[0]} "
                                f"run to expiry ({pos.days_to_expiry} DTE). "
                                f"Currently at {pos.profit_pct:.0%} profit.",
                    tax_impact=0,
                    opportunity_cost=0,
                    rationale="Position working fine. Let theta finish the job."
                ))
    
    # === SHORT-TERM: Route all new trades through the system ===
    plan.short_term_actions.append(TransitionAction(
        urgency="short_term",
        action="reclassify",
        symbol="ALL",
        description="From today forward, ALL new option trades go through the signal "
                    "system. No more manual put/call selling without system confirmation.",
        tax_impact=0,
        opportunity_cost=0,
        rationale="This is how you transition organically — old positions wind down, "
                  "new positions follow the framework."
    ))
    
    # === MEDIUM-TERM: ADBE concentration reduction ===
    adbe_positions = [s for s in intake.stock_positions if s.symbol == "ADBE"]
    if adbe_positions:
        total_adbe_shares = sum(p.shares for p in adbe_positions)
        adbe_value = sum(p.shares * p.current_price for p in adbe_positions)
        adbe_pct = adbe_value / nlv
        
        if adbe_pct > 0.15:
            shares_to_sell = int((adbe_pct - 0.15) * nlv / adbe_positions[0].current_price)
            quarterly_sell = shares_to_sell // 4 + 1
            
            plan.medium_term_actions.append(TransitionAction(
                urgency="medium_term",
                action="close",
                symbol="ADBE",
                description=f"ADBE at {adbe_pct:.0%} of NLV (target 15%). "
                            f"Sell {quarterly_sell} shares per quarter over 4 quarters. "
                            f"Redeploy: 40% Engine 1 diversification, 50% Engine 2, 10% cash.",
                tax_impact=estimate_tax(
                    sum(p.unrealized_pnl for p in adbe_positions) * (shares_to_sell / total_adbe_shares),
                    any(p.is_ltcg for p in adbe_positions)
                ),
                opportunity_cost=0,
                rationale="Concentration risk. Systematic reduction, not panic selling."
            ))
    
    # === MEDIUM-TERM: Build Engine 1 core holdings ===
    plan.medium_term_actions.append(TransitionAction(
        urgency="medium_term",
        action="buy",
        symbol="DIVERSIFICATION",
        description="Use 40% of freed capital (from closed positions + ADBE sells) "
                    "to build Engine 1 core holdings. Priority: names you'd hold 3+ years "
                    "that aren't already in the portfolio. Buy on dips.",
        tax_impact=0,
        opportunity_cost=0,
        rationale="Engine 1 needs building. Use Engine 2 income to fund it."
    ))
    
    # === MEDIUM-TERM: Add tail hedges ===
    for mh in gap.missing_hedges:
        plan.medium_term_actions.append(TransitionAction(
            urgency="medium_term",
            action="buy_hedge",
            symbol="SPY",
            description=f"Buy SPY puts (5% OTM, 45 DTE, rolling monthly). "
                        f"Budget ~{mh['estimated_cost_pct']:.1%} of NLV annually "
                        f"(~${nlv * mh['estimated_cost_pct'] / 12:,.0f}/month).",
            tax_impact=0,
            opportunity_cost=nlv * mh["estimated_cost_pct"],
            rationale="Insurance. Costs 1-2% annually, prevents catastrophic drawdown."
        ))
    
    return plan
```

### Step 4: Telegram Onboarding Flow

```python
async def run_onboarding(bot, chat_id: str):
    """
    Interactive Telegram session that walks the user through:
    1. Account discovery (what accounts, balances, restrictions)
    2. Liquidity preferences (how much must stay accessible)
    3. Position classification (Engine 1 or Engine 2 for each stock)
    
    Total time: ~8-10 minutes.
    """
    
    # Pull portfolio across ALL accounts
    all_accounts = await get_all_etrade_accounts()
    
    # =============================================
    # PHASE 1: Account Discovery
    # =============================================
    
    await bot.send_message(chat_id, f"""
━━ WHEEL COPILOT ONBOARDING ━━

Found {len(all_accounts)} E*Trade accounts. Let me confirm the details.
""")
    
    discovered_accounts = {}
    total_nlv = 0
    
    for acct in all_accounts:
        acct_type_guess = detect_account_type(acct)  # heuristic from E*Trade API
        
        await bot.send_message(chat_id, f"""
📋 Account: {acct['description']} ({acct['account_id_key'][:8]}...)
Balance: ${acct['total_value']:,.0f}
Options Level: {acct.get('options_level', 'Unknown')}

What type of account is this?

[💰 TAXABLE]  [🟣 ROTH IRA]  [🔵 TRAD IRA]  [⚪ OTHER]
""")
        
        response = await wait_for_button(chat_id)
        
        account_type = {
            "taxable": "taxable",
            "roth": "roth_ira", 
            "trad": "traditional_ira",
            "other": "other"
        }.get(response, "taxable")
        
        # Get options level if not auto-detected
        options_level = acct.get('options_level')
        if not options_level:
            await bot.send_message(chat_id, f"""
What options level is approved on this account?

[1️⃣ Level 1] Covered calls only
[2️⃣ Level 2] + Cash-secured puts  
[3️⃣ Level 3] + Spreads
[4️⃣ Level 4] + Naked options, strangles
""")
            level_response = await wait_for_button(chat_id)
            options_level = int(level_response)
        
        discovered_accounts[acct['account_id_key']] = BrokerageAccount(
            account_id=acct['account_id_key'],
            account_type=account_type,
            total_value=acct['total_value'],
            cash_available=acct.get('cash_available', 0),
            buying_power=acct.get('buying_power', 0),
            options_level=options_level,
            margin_enabled=account_type == "taxable",
            can_short_stock=account_type == "taxable",
            withdrawal_restricted=account_type in ("roth_ira", "traditional_ira"),
            early_withdrawal_penalty=0.10 if account_type == "traditional_ira" else 0,
            annual_contribution_limit=7000 if account_type in ("roth_ira", "traditional_ira") else 0,
            contributions_this_year=0,
            remaining_contribution_room=7000 if account_type in ("roth_ira", "traditional_ira") else 0,
            roth_contribution_basis=0,
            roth_earnings=0,
        )
        total_nlv += acct['total_value']
    
    # Roth specifics if applicable
    roth_acct = next((a for a in discovered_accounts.values() 
                      if a.account_type == "roth_ira"), None)
    if roth_acct:
        await bot.send_message(chat_id, f"""
🟣 ROTH IRA: ${roth_acct.total_value:,.0f}

I need to know your contribution basis (total amount you've 
put IN, not including growth). This amount is withdrawable 
anytime without penalty. The rest (earnings) is locked until 59½.

Rough estimate is fine. You can find the exact number on 
your E*Trade tax documents.

How much have you CONTRIBUTED to this Roth (not growth)?

[Type a number like 150000]
""")
        
        contrib_response = await wait_for_text(chat_id)
        roth_acct.roth_contribution_basis = float(contrib_response.replace(",", "").replace("$", ""))
        roth_acct.roth_earnings = roth_acct.total_value - roth_acct.roth_contribution_basis
    
    # =============================================
    # PHASE 2: Liquidity Preferences
    # =============================================
    
    liquid_total = sum(a.liquid_value for a in discovered_accounts.values())
    locked_total = total_nlv - liquid_total
    
    await bot.send_message(chat_id, f"""
━━ LIQUIDITY ASSESSMENT ━━

Total NLV: ${total_nlv:,.0f}
Liquid (accessible now): ${liquid_total:,.0f} ({liquid_total/total_nlv:.0%})
Locked (restricted): ${locked_total:,.0f} ({locked_total/total_nlv:.0%})

I need to know your monthly expenses so I can protect an 
emergency reserve and set liquidity constraints.

What are your approximate monthly expenses?
(Rent/mortgage, bills, food, everything)

[Type a number like 8000]
""")
    
    expenses_response = await wait_for_text(chat_id)
    monthly_expenses = float(expenses_response.replace(",", "").replace("$", ""))
    
    emergency_reserve = monthly_expenses * 6  # 6 months
    
    await bot.send_message(chat_id, f"""
Got it. Setting up:

Emergency reserve: ${emergency_reserve:,.0f} (6 months)
Minimum liquid ratio: 60% of NLV
Current liquid ratio: {liquid_total/total_nlv:.0%} {'✅' if liquid_total/total_nlv >= 0.60 else '⚠️'}

The system will NEVER route trades in a way that pushes 
your liquid assets below ${emergency_reserve:,.0f} or below 
60% of total NLV. This is a hard constraint.

""")
    
    # Build the AccountRouter
    account_router = AccountRouter(
        accounts=discovered_accounts,
        min_liquid_pct=0.60,
        min_liquid_dollars=max(100_000, emergency_reserve),
        emergency_reserve_months=6,
        monthly_expenses=monthly_expenses,
    )
    
    # Show routing summary
    await bot.send_message(chat_id, f"""
━━ ACCOUNT ROUTING PLAN ━━

{account_router.generate_routing_summary()}

{account_router.estimate_annual_tax_savings()}

ROUTING RULES:
• Weekly/monthly puts → Roth IRA (tax-free income)
• Strangles/spreads → Taxable (needs Level 3-4)
• Engine 1 stock buys → Taxable (LTCG treatment)
• Tax-loss harvesting → Taxable only

Now let's classify your positions.
""")
    
    # =============================================
    # PHASE 3: Position Classification (existing code)
    # =============================================
    
    # Pull positions across all accounts
    all_positions = []
    for acct in all_accounts:
        positions = await get_account_positions(acct['account_id_key'])
        for pos in positions:
            pos.account_id = acct['account_id_key']
            pos.account_type = discovered_accounts[acct['account_id_key']].account_type
        all_positions.extend(positions)
    
    intake = auto_classify_portfolio(all_positions)
    
    await bot.send_message(chat_id, f"""
━━ POSITION CLASSIFICATION ━━

Auto-classified:
  {len(intake.short_puts)} short puts → Engine 2
  {len(intake.short_calls)} short calls → Engine 2
  ${intake.cash:,.0f} cash → Engine 3

I need your input on {len(intake.stock_positions)} stock positions.
For each one:

🔵 ENGINE 1 = Long-term hold (3+ years).
   I'll protect upside — calls only on 30-40% of shares.

🟢 ENGINE 2 = Wheel income position.
   I'll sell calls on 100% and maximize premium.

Let's go through them.
""")
    
    # Classify each stock position
    for stock in intake.stock_positions:
        pnl_color = "📈" if stock.unrealized_pnl >= 0 else "📉"
        tax_note = f"LTCG ✅" if stock.is_ltcg else f"STCG ({stock.holding_period_days}d held)"
        
        await bot.send_message(chat_id, f"""
{pnl_color} {stock.symbol}: {stock.shares} shares @ ${stock.current_price:.2f}
P&L: ${stock.unrealized_pnl:+,.0f} ({stock.unrealized_pnl/stock.cost_basis*100:+.0f}%)
Tax: {tax_note}

My suggestion: {stock.suggested_engine.replace('engine', 'Engine ')}
Reason: {stock.suggestion_reason}

[🔵 ENGINE 1]  [🟢 ENGINE 2]
""")
        
        # Wait for response...
        response = await wait_for_button(chat_id)
        stock.engine = "engine1" if response == "engine1" else "engine2"
        
        if stock.engine == "engine1":
            # Ask conviction for Engine 1 positions
            await bot.send_message(chat_id, f"""
{stock.symbol} → Engine 1 (long-term hold)

How convicted are you on this name for the next 3+ years?

[🟢 HIGH — core compounder]
[🟡 MEDIUM — like it but not married]
[⚪ LOW — inherited/unsure, might sell]
""")
            conv_response = await wait_for_button(chat_id)
            stock.conviction = conv_response
    
    # Run gap analysis
    gap = analyze_gaps(intake, nlv)
    
    # Generate transition plan
    plan = generate_transition_plan(intake, gap, nlv)
    
    # Present the plan
    await bot.send_message(chat_id, f"""
━━ ONBOARDING COMPLETE ━━

CURRENT ALLOCATION:
  Engine 1 (Core Holdings): {gap.engine1_current_pct:.0%}
  Engine 2 (Active Wheel):  {gap.engine2_current_pct:.0%}
  Engine 3 (Dry Powder):    {gap.engine3_current_pct:.0%}

TARGET: 45% / 45% / 10%

CRITICAL ACTIONS (this week): {len(plan.immediate_actions)}
{chr(10).join(f'  {a.description}' for a in plan.immediate_actions[:5])}

SHORT-TERM (2-4 weeks): {len(plan.short_term_actions)}
MEDIUM-TERM (1-3 months): {len(plan.medium_term_actions)}

Estimated time to full transition: ~{plan.estimated_transition_weeks} weeks

The system will start generating daily briefings tomorrow.
Old positions will wind down naturally.
New trades follow the signal framework from day 1.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""")
    
    # Save classifications to database
    await save_onboarding_results(intake, gap, plan)
```

### Onboarding SQL Schema

```sql
-- Store the onboarding classification for each position
CREATE TABLE position_classifications (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(10) NOT NULL,
    engine VARCHAR(10) NOT NULL,         -- 'engine1', 'engine2', 'engine3'
    conviction VARCHAR(10),              -- 'high', 'medium', 'low' (Engine 1 only)
    classification_date DATE NOT NULL,
    shares INT,
    cost_basis DECIMAL(12,2),
    classified_by VARCHAR(10) DEFAULT 'user',  -- 'user', 'auto', 'system'
    notes TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Store the transition plan for tracking
CREATE TABLE transition_actions (
    id SERIAL PRIMARY KEY,
    urgency VARCHAR(20) NOT NULL,
    action VARCHAR(20) NOT NULL,
    symbol VARCHAR(10) NOT NULL,
    description TEXT,
    tax_impact DECIMAL(12,2),
    status VARCHAR(20) DEFAULT 'pending',  -- 'pending', 'completed', 'skipped'
    completed_date DATE,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Track wash sale windows
CREATE TABLE wash_sale_tracker (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(10) NOT NULL,
    loss_date DATE NOT NULL,
    loss_amount DECIMAL(12,2),
    wash_sale_window_end DATE NOT NULL,  -- loss_date + 30 days
    is_active BOOLEAN DEFAULT TRUE,      -- false after window expires
    created_at TIMESTAMP DEFAULT NOW()
);
```

### Wash Sale Tracker

```python
@dataclass
class WashSaleTracker:
    """
    Track wash sale windows for every closed-at-a-loss position.
    Warn before opening a new position on the same ticker.
    """
    
    active_windows: dict[str, date]  # symbol → window_end_date
    
    def record_loss(self, symbol: str, loss_date: date, loss_amount: float):
        """Record a closed-at-a-loss trade. Opens a 30-day wash sale window."""
        self.active_windows[symbol] = loss_date + timedelta(days=30)
    
    def check_before_trade(self, symbol: str) -> tuple[bool, str | None]:
        """Check if opening a new position would trigger a wash sale."""
        window_end = self.active_windows.get(symbol)
        if window_end and date.today() <= window_end:
            days_remaining = (window_end - date.today()).days
            return (False, 
                    f"⚠️ WASH SALE: {symbol} was closed at a loss on "
                    f"{window_end - timedelta(days=30)}. "
                    f"New position triggers wash sale rule. "
                    f"Wait {days_remaining} more days or choose a different ticker.")
        return (True, None)
    
    def get_blocked_tickers(self) -> list[str]:
        """Return all tickers currently in a wash sale window."""
        today = date.today()
        return [sym for sym, end in self.active_windows.items() if end >= today]
```

---

## Data Collection Layer

### 1. Broker API — Portfolio State

**Recommended: Interactive Brokers (IBKR) Client Portal API or TWS API**

IBKR is preferred because it exposes real-time Greeks, IV, and supports programmatic order submission for Phase 4. If your positions are at Schwab, Schwab's API works for positions/balances but lacks real-time options Greeks — you'd supplement with market data APIs.

```python
# Core data to pull per position
@dataclass
class Position:
    symbol: str
    position_type: str          # "short_put", "short_call", "long_stock"
    quantity: int
    strike: float
    expiration: date
    entry_price: float          # premium received
    current_price: float        # current option mark
    underlying_price: float
    cost_basis: float           # for stock positions (assignment cost basis)
    
    # Greeks (from broker or calculated)
    delta: float
    theta: float
    gamma: float
    vega: float
    iv: float                   # implied volatility of the specific contract
    
    # Derived
    days_to_expiry: int
    distance_from_strike_pct: float  # how far OTM as %
    profit_pct: float               # current P&L as % of max profit
    max_profit: float               # premium received
    max_loss: float                 # strike - premium (puts) or unlimited (naked calls)
```

```python
# Portfolio-level aggregation
@dataclass  
class PortfolioState:
    positions: list[Position]
    cash_available: float
    buying_power: float
    net_liquidation: float
    portfolio_delta: float      # sum of position deltas
    portfolio_theta: float      # daily theta income
    portfolio_vega: float       # sensitivity to IV changes
    concentration: dict         # {symbol: % of portfolio}
    sector_exposure: dict       # {sector: % of portfolio}
```

### 2. Market Data — IV Context

IV rank and IV percentile are critical for wheel timing. You sell premium when IV is high.

```python
@dataclass
class MarketContext:
    symbol: str
    iv_rank: float              # current IV vs 52-week range (0-100)
    iv_percentile: float        # % of days IV was below current (0-100)
    iv_rank_change_5d: float    # how much IV rank moved in 5 days
    iv_30d: float               # 30-day implied volatility
    hv_30d: float               # 30-day historical (realized) volatility
    iv_hv_spread: float         # iv_30d - hv_30d (positive = IV rich)
    
    # Price context
    price: float
    price_change_1d: float      # % change today
    price_change_5d: float      # % change over 5 days
    price_vs_52w_high: float    # % below 52-week high
    price_vs_200sma: float      # % above/below 200 SMA
    
    # Volume/flow
    put_call_ratio: float
    option_volume_vs_avg: float # today's option volume vs 20-day avg
    
    # Macro (shared across all symbols)
    vix: float | None           # current VIX level
    vix_change_1d: float | None # VIX point change today
    vix_term_structure: str | None  # "contango" or "backwardation"


@dataclass
class PriceHistory:
    """Technical context for strike selection and signal detection."""
    symbol: str
    current_price: float
    
    # Moving averages
    sma_200: float | None
    sma_50: float | None
    sma_20: float | None
    ema_9: float | None
    
    # Key levels
    high_52w: float
    low_52w: float
    recent_swing_high: float | None   # highest point in last 20 days
    recent_swing_low: float | None    # lowest point in last 20 days
    anchored_vwap_90d: float | None   # 90-day anchored VWAP
    
    # Momentum / mean reversion
    rsi_14: float | None              # RSI(14), <30 = oversold, >70 = overbought
    
    # History arrays
    daily_closes: list[float]         # last 252 closes for calculations
    daily_volumes: list[float]        # last 252 volumes
    
    def last_n_closes(self, n: int) -> list[float]:
        return self.daily_closes[-n:]
    
    def consecutive_red_days(self) -> int:
        count = 0
        closes = self.daily_closes
        for i in range(len(closes) - 1, 0, -1):
            if closes[i] < closes[i-1]:
                count += 1
            else:
                break
        return count
    
    def drawdown_from_n_day_high(self, n: int) -> float:
        recent = self.daily_closes[-n:]
        peak = max(recent)
        return (peak - self.current_price) / peak * 100
```

**Data sources:**

| Data Point | Source | Cost |
|---|---|---|
| IV Rank / Percentile | Calculate from `yfinance` historical IV (252 trading days) | Free |
| Greeks | E*Trade options chain API (`OptionGreeks` in response) | Free with broker |
| Price / Volume / SMAs | E*Trade quote API or `yfinance` | Free |
| RSI, swing highs/lows | Calculate from `yfinance` daily closes | Free |
| VIX + term structure | `yfinance` (`^VIX`, `^VIX3M`) | Free |
| Put/Call ratio | CBOE, or scrape from barchart.com | Free |
| Sector classification | Static mapping or `yfinance` `.info['sector']` | Free |
| Anchored VWAP | Calculate from `yfinance` price + volume data | Free |

### 3. Events Calendar

Earnings and ex-dividend dates are the two events that break wheel mechanics. You never want to sell a call through earnings (IV crush risk), and you never want to be short a call through ex-div on a stock you want to keep.

```python
@dataclass
class EventCalendar:
    symbol: str
    next_earnings: date | None
    earnings_confirmed: bool    # estimated vs confirmed
    next_ex_dividend: date | None
    dividend_amount: float | None
    
    # Macro events (shared across all positions)
    fed_meeting: date | None
    fed_speakers_today: list[str]
    cpi_ppi_date: date | None
    major_macro_event: str | None  # e.g., "FOMC minutes", "jobs report"
```

**Data sources:**

| Data Point | Source |
|---|---|
| Earnings dates | `yfinance` Ticker.calendar, or earnings-whispers API |
| Ex-dividend dates | `yfinance` Ticker.dividends, or dividend.com |
| Fed calendar | federalreserve.gov (static scrape, updates monthly) |
| Economic calendar | tradingeconomics.com API or investing.com scrape |

---

## Analysis Engine

### Layer 0: Alpha Signals (runs before everything else)

This is the edge layer. A pro doesn't sell puts randomly — they hunt for dislocations and attack them.

```python
@dataclass
class AlphaSignal:
    symbol: str
    signal_type: str
    strength: float          # 0-100, used for position sizing
    direction: str           # "sell_put", "sell_call", "sit"
    reasoning: str
    expires: datetime        # signal decays — when does this stop being valid?


class SignalType(Enum):
    # === DIP DETECTION ===
    INTRADAY_DIP = "intraday_dip"               # stock down 3%+ today, no fundamental reason
    MULTI_DAY_PULLBACK = "multi_day_pullback"    # 3+ red days, approaching support
    SECTOR_ROTATION_DIP = "sector_rotation"      # sector down but name fundamentals intact
    EARNINGS_OVERREACTION = "earnings_overreaction"  # post-earnings dump >5%, guidance intact
    MACRO_FEAR_SPIKE = "macro_fear_spike"        # broad selloff, VIX spike, everything cheap
    
    # === IV SURFACE DISLOCATIONS ===
    IV_RANK_SPIKE = "iv_rank_spike"              # IV rank jumped >20pts in 1 day
    SKEW_BLOW_OUT = "skew_blowout"               # put skew elevated vs historical norm
    TERM_STRUCTURE_INVERSION = "term_inversion"  # front month IV > back month (fear)
    IV_CRUSH_SETUP = "iv_crush_setup"            # pre-earnings IV inflated, sell before
    
    # === TECHNICAL LEVELS ===
    SUPPORT_BOUNCE = "support_bounce"            # price touching 200 SMA, 52w trendline, etc.
    OVERSOLD_RSI = "oversold_rsi"                # RSI <30, mean reversion candidate
    VOLUME_CLIMAX = "volume_climax"              # capitulation volume spike on down move
    GAP_FILL = "gap_fill"                        # price filling a prior gap — support zone
    
    # === FLOW / SENTIMENT ===
    UNUSUAL_PUT_SELLING = "unusual_put_selling"  # smart money selling puts = bullish
    DARK_POOL_ACCUMULATION = "dark_pool"         # large block prints at/above ask
    SHORT_INTEREST_SQUEEZE = "short_squeeze"     # high SI + price uptick = squeeze setup
```

```python
def detect_dip_signals(symbol: str, mkt: MarketContext, hist: PriceHistory) -> list[AlphaSignal]:
    """
    The core edge: sell puts INTO weakness, not into strength.
    A pro gets greedy when others are fearful.
    """
    signals = []
    
    # === INTRADAY DIP ===
    # Stock down 2.5%+ today with no earnings/news catalyst
    # IV spikes on down moves → fatter premiums exactly when you want to sell
    if mkt.price_change_1d <= -2.5:
        strength = min(100, abs(mkt.price_change_1d) * 15)  # -3% = 45, -5% = 75
        
        # Boost signal if IV also spiked (confirms fear premium)
        if mkt.iv_rank > 50:
            strength = min(100, strength * 1.3)
        
        signals.append(AlphaSignal(
            symbol=symbol,
            signal_type=SignalType.INTRADAY_DIP,
            strength=strength,
            direction="sell_put",
            reasoning=f"{symbol} down {mkt.price_change_1d:.1f}% today, "
                      f"IV rank {mkt.iv_rank:.0f}. Sell into fear.",
            expires=datetime.now() + timedelta(hours=24)
        ))
    
    # === MULTI-DAY PULLBACK ===
    # 3+ consecutive red days, stock down >5% from recent high
    # This is the bread-and-butter dip buy for wheel traders
    recent_closes = hist.last_n_closes(5)
    red_days = sum(1 for i in range(1, len(recent_closes)) 
                   if recent_closes[i] < recent_closes[i-1])
    drawdown_from_5d_high = (max(recent_closes) - recent_closes[-1]) / max(recent_closes) * 100
    
    if red_days >= 3 and drawdown_from_5d_high >= 5.0:
        strength = min(100, drawdown_from_5d_high * 10 + red_days * 5)
        signals.append(AlphaSignal(
            symbol=symbol,
            signal_type=SignalType.MULTI_DAY_PULLBACK,
            strength=strength,
            direction="sell_put",
            reasoning=f"{symbol}: {red_days} red days, down {drawdown_from_5d_high:.1f}% "
                      f"from 5-day high. Approaching support.",
            expires=datetime.now() + timedelta(hours=48)
        ))
    
    # === SUPPORT BOUNCE ===
    # Price within 2% of 200-day SMA, 50-day SMA, or 52-week low
    # These are where institutions add — sell puts at their entry
    support_levels = {
        "200 SMA": hist.sma_200,
        "50 SMA": hist.sma_50,
        "52w low": hist.low_52w,
    }
    
    for level_name, level_price in support_levels.items():
        if level_price and 0 < (mkt.price - level_price) / level_price * 100 < 3.0:
            signals.append(AlphaSignal(
                symbol=symbol,
                signal_type=SignalType.SUPPORT_BOUNCE,
                strength=70,
                direction="sell_put",
                reasoning=f"{symbol} at ${mkt.price:.2f}, within 3% of {level_name} "
                          f"(${level_price:.2f}). Sell puts at/below support.",
                expires=datetime.now() + timedelta(hours=48)
            ))
    
    # === OVERSOLD RSI ===
    if hist.rsi_14 and hist.rsi_14 < 30:
        signals.append(AlphaSignal(
            symbol=symbol,
            signal_type=SignalType.OVERSOLD_RSI,
            strength=min(100, (30 - hist.rsi_14) * 5 + 50),
            direction="sell_put",
            reasoning=f"{symbol} RSI(14) at {hist.rsi_14:.1f} — oversold. "
                      f"Mean reversion likely. Aggressive put sale.",
            expires=datetime.now() + timedelta(hours=72)
        ))
    
    # === MACRO FEAR SPIKE ===
    # VIX >25 and rising → sell premium on everything, this is harvest time
    if mkt.vix and mkt.vix > 25 and mkt.vix_change_1d > 2:
        signals.append(AlphaSignal(
            symbol=symbol,
            signal_type=SignalType.MACRO_FEAR_SPIKE,
            strength=min(100, (mkt.vix - 20) * 5),
            direction="sell_put",
            reasoning=f"VIX at {mkt.vix:.1f} (+{mkt.vix_change_1d:.1f} today). "
                      f"Fear premium elevated across the board. Sell aggressively.",
            expires=datetime.now() + timedelta(hours=24)
        ))
    
    return signals


def detect_iv_surface_signals(symbol: str, mkt: MarketContext, chain: OptionsChain) -> list[AlphaSignal]:
    """
    IV surface analysis — this is where sophisticated traders find edge.
    """
    signals = []
    
    # === IV RANK SPIKE ===
    # IV rank jumped significantly → premium just got richer
    if mkt.iv_rank > 60 and mkt.iv_rank_change_5d > 20:
        signals.append(AlphaSignal(
            symbol=symbol,
            signal_type=SignalType.IV_RANK_SPIKE,
            strength=min(100, mkt.iv_rank),
            direction="sell_put",
            reasoning=f"{symbol} IV rank spiked to {mkt.iv_rank:.0f} "
                      f"(+{mkt.iv_rank_change_5d:.0f} in 5 days). Premium is rich.",
            expires=datetime.now() + timedelta(hours=48)
        ))
    
    # === PUT SKEW BLOWOUT ===
    # When put skew is steep, OTM puts are overpriced relative to ATM
    # This means your 25-delta put is collecting MORE premium than usual
    atm_iv = chain.atm_iv
    otm_put_iv = chain.get_iv_at_delta(-0.25)
    if atm_iv and otm_put_iv:
        skew = (otm_put_iv - atm_iv) / atm_iv * 100
        historical_skew = chain.historical_skew_25d  # avg skew over 25 days
        
        if historical_skew and skew > historical_skew * 1.3:  # 30%+ above normal
            signals.append(AlphaSignal(
                symbol=symbol,
                signal_type=SignalType.SKEW_BLOW_OUT,
                strength=65,
                direction="sell_put",
                reasoning=f"{symbol} put skew at {skew:.1f}% vs {historical_skew:.1f}% normal. "
                          f"OTM puts overpriced — sell the fear.",
                expires=datetime.now() + timedelta(hours=48)
            ))
    
    # === TERM STRUCTURE INVERSION ===
    # Front month IV > back month = market pricing near-term fear
    # Sell front month to capture the inversion premium
    front_iv = chain.iv_by_expiry.get("front_month")
    back_iv = chain.iv_by_expiry.get("second_month")
    if front_iv and back_iv and front_iv > back_iv * 1.05:
        signals.append(AlphaSignal(
            symbol=symbol,
            signal_type=SignalType.TERM_STRUCTURE_INVERSION,
            strength=60,
            direction="sell_put",
            reasoning=f"{symbol} term structure inverted: front IV {front_iv:.1f}% vs "
                      f"back {back_iv:.1f}%. Sell front month for inversion premium.",
            expires=datetime.now() + timedelta(hours=24)
        ))
    
    # === PRE-EARNINGS IV CRUSH SETUP ===
    # IV inflated 5-10 days before earnings → sell premium that will 
    # get crushed post-report. Close the day before earnings.
    # This is NOT holding through earnings — it's capturing the IV runup.
    cal = get_event_calendar(symbol)
    if cal.next_earnings:
        days_to_earnings = (cal.next_earnings - date.today()).days
        if 5 <= days_to_earnings <= 15 and mkt.iv_rank > 65:
            signals.append(AlphaSignal(
                symbol=symbol,
                signal_type=SignalType.IV_CRUSH_SETUP,
                strength=55,
                direction="sell_put",
                reasoning=f"{symbol} earnings in {days_to_earnings}d, IV rank {mkt.iv_rank:.0f}. "
                          f"Sell premium now, close day before report. Capture IV decay.",
                expires=cal.next_earnings - timedelta(days=1)
            ))
    
    return signals
```

### Layer 1: Smart Strike Selection

A pro doesn't just pick the nearest delta — they pick strikes at meaningful levels.

```python
@dataclass
class SmartStrike:
    strike: float
    delta: float
    premium: float
    yield_on_capital: float
    annualized_yield: float
    technical_reason: str | None    # why this strike matters
    strike_score: float             # composite score for ranking


def find_smart_strikes(
    symbol: str,
    chain: OptionsChain,
    hist: PriceHistory,
    direction: str,         # "sell_put" or "sell_call"
    params: TradingParams
) -> list[SmartStrike]:
    """
    Strike selection using technical levels, not just delta.
    
    For puts: find strikes AT or BELOW key support levels.
    For calls: find strikes AT or ABOVE key resistance levels.
    
    A strike at support means: if you get assigned, you're buying 
    where institutions are buying. That's not a loss — it's a discount.
    """
    
    # Identify key levels
    support_levels = [
        ("200 SMA", hist.sma_200),
        ("50 SMA", hist.sma_50),
        ("Prior low", hist.recent_swing_low),
        ("52w low", hist.low_52w),
        ("VWAP anchor", hist.anchored_vwap_90d),
        ("Round number", round_down_to_5(hist.current_price * 0.90)),
    ]
    
    resistance_levels = [
        ("52w high", hist.high_52w),
        ("Prior high", hist.recent_swing_high),
        ("Round number", round_up_to_5(hist.current_price * 1.10)),
    ]
    
    levels = support_levels if direction == "sell_put" else resistance_levels
    
    # Score each available strike
    scored_strikes = []
    target_expiry = chain.get_expiry_near_dte(params.sweet_spot_dte)
    
    for contract in chain.filter(expiry=target_expiry, option_type=direction):
        if not (params.put_min_delta <= contract.delta <= params.put_max_delta):
            continue
        
        score = 0
        technical_reason = None
        
        # Base score from yield
        yoc = contract.mid_price / (contract.strike * 100)
        annualized = yoc * (365 / contract.dte)
        score += annualized * 200  # weight yield heavily
        
        # Bonus: strike aligns with a technical level
        for level_name, level_price in levels:
            if level_price and abs(contract.strike - level_price) / level_price < 0.01:
                score += 25
                technical_reason = f"Strike at {level_name} (${level_price:.2f})"
                break
        
        # Bonus: strike is below ALL support levels (maximum safety)
        if direction == "sell_put":
            levels_below = sum(1 for _, lp in support_levels 
                             if lp and contract.strike < lp)
            score += levels_below * 5
        
        # Bonus: high open interest at this strike (institutional activity)
        if contract.open_interest > 1000:
            score += 10
        
        # Penalty: strike too close to current price
        distance_pct = abs(contract.strike - hist.current_price) / hist.current_price * 100
        if distance_pct < 5:
            score -= 15
        
        scored_strikes.append(SmartStrike(
            strike=contract.strike,
            delta=contract.delta,
            premium=contract.mid_price,
            yield_on_capital=yoc,
            annualized_yield=annualized,
            technical_reason=technical_reason,
            strike_score=score
        ))
    
    return sorted(scored_strikes, key=lambda s: s.strike_score, reverse=True)
```

### Layer 2: Conviction-Based Position Sizing

A pro doesn't bet the same amount every time. Size up when the setup is fat, size down when it's marginal.

```python
@dataclass
class SizedOpportunity:
    """An opportunity with a concrete position size based on conviction."""
    symbol: str
    trade_type: str
    strike: float
    expiration: date
    premium: float
    contracts: int                  # how many to sell
    capital_deployed: float         # total capital at risk
    portfolio_pct: float            # % of NLV this trade represents
    yield_on_capital: float
    annualized_yield: float
    conviction: str                 # "high", "medium", "low"
    signals: list[AlphaSignal]      # what triggered this
    smart_strike: SmartStrike       # why this strike
    reasoning: str                  # full narrative


def size_position(
    opportunity: Opportunity,
    signals: list[AlphaSignal],
    portfolio: PortfolioState,
    params: TradingParams
) -> SizedOpportunity:
    """
    Conviction-based sizing. Not fixed-fraction — adaptive.
    
    HIGH conviction (signal strength >70, multiple confirming signals):
      → 3-5% of NLV per trade
      → Example: stock down 5%, RSI <30, IV rank >70, at 200 SMA
      
    MEDIUM conviction (signal strength 40-70, or single strong signal):
      → 1.5-3% of NLV per trade
      → Example: IV rank >50, decent yield, no special dip
      
    LOW conviction (signal strength <40, marginal setup):
      → 0.5-1.5% of NLV per trade
      → Example: watchlist name, IV rank barely above threshold
    """
    
    # Aggregate signal strength
    avg_strength = sum(s.strength for s in signals) / len(signals) if signals else 30
    num_confirming = len(signals)
    
    # Conviction classification
    if avg_strength > 70 and num_confirming >= 2:
        conviction = "high"
        target_pct = 0.04  # 4% of NLV
    elif avg_strength > 50 or num_confirming >= 2:
        conviction = "medium"
        target_pct = 0.02  # 2% of NLV
    else:
        conviction = "low"
        target_pct = 0.01  # 1% of NLV
    
    # Adjust for concentration limits
    current_exposure = portfolio.concentration.get(opportunity.symbol, 0)
    max_allowed = params.max_concentration_per_symbol
    remaining_room = max(0, max_allowed - current_exposure)
    target_pct = min(target_pct, remaining_room)
    
    # Adjust for margin utilization
    if portfolio.margin_utilization > 0.40:
        target_pct *= 0.5  # cut size in half when margin is getting full
    
    # Calculate contracts
    capital_per_contract = opportunity.strike * 100
    total_capital = portfolio.net_liquidation * target_pct
    contracts = max(1, int(total_capital / capital_per_contract))
    
    return SizedOpportunity(
        symbol=opportunity.symbol,
        trade_type=opportunity.trade_type,
        strike=opportunity.strike,
        expiration=opportunity.expiration,
        premium=opportunity.premium,
        contracts=contracts,
        capital_deployed=contracts * capital_per_contract,
        portfolio_pct=contracts * capital_per_contract / portfolio.net_liquidation,
        yield_on_capital=opportunity.yield_on_capital,
        annualized_yield=opportunity.annualized_yield,
        conviction=conviction,
        signals=signals,
        smart_strike=opportunity.smart_strike,
        reasoning=build_reasoning(opportunity, signals, conviction, contracts)
    )
```

### Layer 3: Position Scanner (existing positions)

```python
class PositionAction(Enum):
    LET_EXPIRE = "let_expire"           # >80% profit, <5 DTE, far OTM
    CLOSE_EARLY = "close_early"         # >50% profit, high gamma risk
    CLOSE_AND_RELOAD = "close_reload"   # >50% profit AND a dip signal exists → recycle capital
    ROLL_OUT = "roll_out"               # approaching strike, want to defend
    ROLL_OUT_AND_UP = "roll_out_up"     # calls: stock rallied past strike
    ROLL_OUT_AND_DOWN = "roll_out_down" # puts: stock dropped near strike  
    ROLL_DOWN_AGGRESSIVE = "roll_down"  # stock crashed — roll to lower strike for net credit
    TAKE_ASSIGNMENT = "take_assignment" # ITM, want to own at this price — then sell calls
    DOUBLE_DOWN = "double_down"         # stock dipped hard, sell MORE puts at lower strike
    ALERT_EARNINGS = "alert_earnings"   # earnings within DTE window
    ALERT_DIVIDEND = "alert_dividend"   # ex-div within DTE window
    MONITOR = "monitor"                 # no action needed


def scan_position(
    pos: Position, 
    mkt: MarketContext, 
    cal: EventCalendar,
    signals: list[AlphaSignal]
) -> tuple[PositionAction, str]:
    """
    Enhanced scanner that incorporates alpha signals.
    Key insight: if a position hit 50% profit AND there's a fresh dip signal
    on another name, close early and redeploy the capital into the dip.
    """
    
    # === AGGRESSIVE: Close and reload ===
    # You have a winner AND there's a better use of that capital right now
    pending_dip_signals = [s for s in signals if s.strength > 60 
                          and s.symbol != pos.symbol]
    
    if pos.profit_pct >= 0.40 and len(pending_dip_signals) > 0:
        best_signal = max(pending_dip_signals, key=lambda s: s.strength)
        return (PositionAction.CLOSE_AND_RELOAD,
                f"Close at {pos.profit_pct:.0%} profit, redeploy into "
                f"{best_signal.symbol} ({best_signal.reasoning})")
    
    # === Profit target hit ===
    if pos.profit_pct >= 0.50 and pos.days_to_expiry > 21:
        return (PositionAction.CLOSE_EARLY,
                f"{pos.profit_pct:.0%} of max profit captured, {pos.days_to_expiry} DTE remaining")
    
    # === Near-expiry and far OTM ===
    if pos.days_to_expiry <= 5 and abs(pos.delta) < 0.10:
        return (PositionAction.LET_EXPIRE, "Far OTM, <5 DTE, let theta finish the job")
    
    # === AGGRESSIVE: Double down on dips ===
    # Your short put is under pressure BUT you believe in the name
    # AND there's a dip signal confirming the drop is overdone
    own_dip_signals = [s for s in signals if s.symbol == pos.symbol 
                       and s.strength > 50]
    
    if (pos.position_type == "short_put" 
        and pos.distance_from_strike_pct < 5.0 
        and pos.distance_from_strike_pct > 0  # still OTM
        and len(own_dip_signals) > 0
        and mkt.iv_rank > 50):
        return (PositionAction.DOUBLE_DOWN,
                f"{pos.symbol} testing your strike but dip signals confirm oversold. "
                f"Sell additional puts at lower strike for net credit.")
    
    # === Approaching strike — roll or take assignment ===
    if pos.position_type == "short_put" and pos.distance_from_strike_pct < 3.0:
        return (PositionAction.TAKE_ASSIGNMENT,
                f"Near strike. Take assignment and immediately sell calls against shares.")
    
    if pos.position_type == "short_call" and pos.distance_from_strike_pct < 2.0:
        return (PositionAction.ROLL_OUT_AND_UP,
                f"Stock approaching call strike. Roll out and up for credit.")
    
    # === Earnings conflict ===
    if cal.next_earnings and cal.next_earnings <= pos.expiration:
        days_to_er = (cal.next_earnings - date.today()).days
        return (PositionAction.ALERT_EARNINGS,
                f"Earnings in {days_to_er} days, before your {pos.expiration} expiry")
    
    # === Dividend conflict (calls only) ===
    if pos.position_type == "short_call":
        if cal.next_ex_dividend and cal.next_ex_dividend <= pos.expiration:
            return (PositionAction.ALERT_DIVIDEND,
                    f"Ex-div before expiry, early assignment risk on calls")
    
    return (PositionAction.MONITOR, "Position healthy, no action needed")
```

### Layer 4: Opportunity Scorer (the main ranking engine)

```python
def find_and_rank_opportunities(
    watchlist: list[str],
    portfolio: PortfolioState,
    params: TradingParams
) -> list[SizedOpportunity]:
    """
    The full pipeline: detect signals → find smart strikes → 
    size by conviction → rank by risk-adjusted expected value.
    """
    all_opportunities = []
    
    for symbol in watchlist:
        mkt = get_market_context(symbol)
        hist = get_price_history(symbol)
        chain = get_options_chain(symbol)
        cal = get_event_calendar(symbol)
        
        # Detect all alpha signals for this name
        dip_signals = detect_dip_signals(symbol, mkt, hist)
        iv_signals = detect_iv_surface_signals(symbol, mkt, chain)
        all_signals = dip_signals + iv_signals
        
        # No signals AND IV rank below threshold → skip entirely
        if not all_signals and mkt.iv_rank < params.min_iv_rank:
            continue
        
        # Hard skip: earnings within expiry window (unless IV crush play)
        iv_crush_signals = [s for s in all_signals 
                          if s.signal_type == SignalType.IV_CRUSH_SETUP]
        if (cal.next_earnings 
            and days_until(cal.next_earnings) < params.max_dte
            and not iv_crush_signals):
            continue
        
        # Find best strikes using technical levels
        direction = "sell_put"  # default for wheel
        smart_strikes = find_smart_strikes(symbol, chain, hist, direction, params)
        
        if not smart_strikes:
            continue
        
        best_strike = smart_strikes[0]
        
        # Build opportunity and size it
        opp = Opportunity(
            symbol=symbol,
            trade_type=direction,
            strike=best_strike.strike,
            expiration=chain.get_expiry_near_dte(params.sweet_spot_dte),
            premium=best_strike.premium,
            yield_on_capital=best_strike.yield_on_capital,
            annualized_yield=best_strike.annualized_yield,
            iv_rank=mkt.iv_rank,
            delta=best_strike.delta,
            smart_strike=best_strike,
            signals=all_signals
        )
        
        sized = size_position(opp, all_signals, portfolio, params)
        all_opportunities.append(sized)
    
    # Rank by composite score: conviction * annualized_yield * signal_strength
    def composite_score(opp: SizedOpportunity) -> float:
        conviction_mult = {"high": 3.0, "medium": 1.5, "low": 1.0}[opp.conviction]
        signal_avg = (sum(s.strength for s in opp.signals) / len(opp.signals) 
                     if opp.signals else 30)
        return opp.annualized_yield * conviction_mult * (signal_avg / 50)
    
    return sorted(all_opportunities, key=composite_score, reverse=True)
```

### Layer 5: Risk / Concentration Monitor

```python
@dataclass
class RiskReport:
    # Concentration
    adbe_pct: float                    # track this specifically given your RSU/ESPP
    top_5_concentration: float          # % in top 5 positions
    sector_breakdown: dict[str, float]  # sector → % of NLV
    
    # Greeks summary
    portfolio_delta_dollars: float      # dollar delta (how much you make/lose per 1% move)
    daily_theta: float                  # what the portfolio earns per day from decay
    portfolio_beta: float               # beta-weighted delta vs SPY
    
    # Stress scenarios
    impact_5pct_down: float            # estimated P&L if market drops 5%
    impact_10pct_down: float           # estimated P&L if market drops 10%
    impact_iv_spike_20pct: float       # estimated P&L if IV spikes 20%
    
    # Alerts
    concentration_warnings: list[str]  # any symbol >10%, sector >35%
    margin_utilization: float          # current margin used / available
    
    # Aggressive metrics
    capital_efficiency: float          # theta per dollar of buying power used
    idle_capital_pct: float            # % of NLV not deployed — money doing nothing
    days_since_last_trade: int         # how long since you last opened a position
```

---

## Claude Reasoning Layer

The analysis engine produces structured data. Claude synthesizes it into a natural language briefing with the mindset of an aggressive, disciplined options trader.

```python
import anthropic

def generate_briefing(
    portfolio: PortfolioState,
    actions: list[tuple[Position, PositionAction, str]],
    opportunities: list[SizedOpportunity],
    risk: RiskReport,
    signals: list[AlphaSignal],
    macro: MacroContext
) -> str:
    
    client = anthropic.Anthropic()
    
    system_prompt = """You are an aggressive, professional options trader managing a 
    wheel strategy portfolio. You think like a market maker who happens to have 
    directional conviction. Your job is to produce a concise morning briefing with 
    concrete, sized trade recommendations.
    
    Your trading philosophy:
    - SELL INTO FEAR. Red days are paydays. When VIX spikes and stocks dip, 
      premiums get fat — that's when you load up, not when you hide.
    - EVERY DOLLAR should be working. Idle cash is a losing position (inflation 
      eats it). Flag idle capital as a problem, not a safety margin.
    - SIZE BY CONVICTION. A high-signal dip setup gets 3-5% of NLV. A marginal 
      IV rank play gets 1%. Never flat-size everything the same.
    - PICK STRIKES AT LEVELS, not at deltas. Selling a put at the 200 SMA with 
      -0.30 delta is a better trade than selling at -0.25 delta in no-man's land.
    - CLOSE WINNERS AND REDEPLOY. A position at 50% profit with 30 DTE remaining 
      is dead capital. Close it and recycle into a fresh high-conviction setup.
    - ASSIGNMENT IS NOT A LOSS. Getting put stock at a support level you chose is 
      the plan working. Immediately sell calls against the shares.
    - RESPECT EARNINGS AND MACRO. Be aggressive on individual names but never 
      ignore the regime. FOMC days, CPI prints, and earnings can gap you.
    
    Output rules:
    - Lead with the alpha: what dips/signals fired today?
    - Give exact strikes, expirations, premiums, contract counts, and order types
    - For every opportunity, state the conviction level and WHY (which signals)
    - Nag about idle capital and capital efficiency (theta per dollar deployed)
    - Flag ADBE concentration relentlessly
    - Be blunt and direct. No hedging language. "Consider" means "do this."
    """
    
    user_prompt = f"""Generate my morning briefing for {today}.

## Active Alpha Signals
{format_signals(signals)}

## Portfolio State
{format_portfolio(portfolio)}

## Position Actions
{format_actions(actions)}

## Sized Opportunities (ranked by composite score)
{format_opportunities(opportunities)}

## Risk Report
{format_risk(risk)}

## Macro Context
{format_macro(macro)}

Produce:
1. SIGNAL FLASH (what's on fire today — dips, IV spikes, fear)
2. ATTACK PLAN (top 3 new trades with exact sizing, in priority order)
3. POSITION MANAGEMENT (closes, rolls, reloads)
4. PORTFOLIO SCORECARD (theta/day, capital efficiency, idle %, concentration)
5. REGIME (attack / hold / defend — one word, then why)
"""
    
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=3000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}]
    )
    
    return response.content[0].text
```

### Example Output

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHEEL COPILOT — Monday, April 13, 2026
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

REGIME: ATTACK 🔥
VIX 24.8 (+3.2 today). Tariff fears hitting semis. 
This is exactly when you sell premium.

━━ SIGNAL FLASH ━━

🔴 NVDA -4.2% today, RSI(14) 28, testing 200 SMA ($98.40)
   IV rank spiked to 78 (+22 in 5 days). Put skew blown out.
   → 3 confirming signals. HIGH CONVICTION.

🔴 AVGO -3.1%, 4 red days, down 8% from 5-day high
   IV rank 71. Approaching 50 SMA support ($162).
   → 2 confirming signals. HIGH CONVICTION.

🟡 AMD -1.8%, IV rank crossed 55. Modest setup.
   → 1 signal. MEDIUM CONVICTION.

━━ ATTACK PLAN ━━

1. SELL 3x NVDA May-16 95P @ $4.85 ($1,455 premium)
   → Strike at 200 SMA ($98.40), delta -0.28
   → Yield 5.1% / 39.6% annualized
   → Capital at risk: $28,500 (3.8% of NLV)
   → CONVICTION: HIGH — dip + RSI + IV spike + support level
   → If assigned, you own NVDA at $90.15 cost basis. Then sell calls.

2. SELL 2x AVGO May-16 155P @ $5.60 ($1,120 premium)
   → Strike below 50 SMA ($162), delta -0.25
   → Yield 3.6% / 28.0% annualized
   → Capital at risk: $31,000 (4.1% of NLV)
   → CONVICTION: HIGH — multi-day pullback + IV spike

3. SELL 1x AMD May-16 135P @ $2.80 ($280 premium)
   → Delta -0.22, IV rank 55
   → Yield 2.1% / 16.1% annualized
   → Capital at risk: $13,500 (1.8% of NLV)
   → CONVICTION: MEDIUM — IV decent but no dip confirmation

Total new premium: $2,855 | New capital deployed: $73,000

━━ POSITION MANAGEMENT ━━

1. CLOSE & RELOAD: GOOG May-02 155P @ $0.45 (entered $3.10)
   → 85% profit captured, 19 DTE. Dead theta.
   → Free up $15,500 → redeploy into NVDA or AVGO above.

2. CLOSE & RELOAD: META May-02 440P @ $1.20 (entered $4.80)
   → 75% profit, 19 DTE. Close and recycle.

3. ⚠️ EARNINGS: AMZN reports Apr 24 (11 days)
   → Your May-02 180P is 62% profit. Close NOW.
   → Do not hold short puts through earnings on a $2T company.

4. DOUBLE DOWN: PLTR May-16 22P (existing) under pressure
   → PLTR at $23.10, testing strike. But RSI 31 + IV rank 68.
   → Sell 2x additional PLTR May-16 20P @ $0.85 for $170.
   → Lowers your effective cost basis if assigned on either leg.

━━ PORTFOLIO SCORECARD ━━

Daily theta:       +$186 → +$224 after today's trades ($6,720/month)
Capital efficiency: $0.38 theta per $100 deployed → $0.44 after
Idle capital:       18% of NLV ⚠️ (target <10% in this regime)
Margin utilization: 34% → 44% after proposed trades
Portfolio delta:    +420 (bullish, appropriate for ATTACK regime)
Beta-weighted Δ:    +340 SPY-equivalent shares

⚠️ ADBE concentration: 31% of NLV. SELL CALLS AGAINST IT.
   → SELL 2x ADBE May-16 440C @ $6.80 ($1,360 premium)
   → Reduces delta exposure and generates income on dead weight.

Stress test (after proposed trades):
  SPY -5%:  -$24,800 | SPY -10%: -$52,100
  VIX +30%: -$5,200  | Max theta drag if flat: +$6,720/month

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## Delivery Layer

### Telegram Bot (Primary)

Telegram is ideal for push notifications on mobile. Simple to set up, supports markdown formatting.

```python
import telegram

async def send_briefing(briefing: str):
    bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
    
    # Split if > 4096 chars (Telegram limit)
    chunks = split_message(briefing, max_len=4096)
    for chunk in chunks:
        await bot.send_message(
            chat_id=YOUR_CHAT_ID,
            text=chunk,
            parse_mode="Markdown"
        )
```

### Trade Log (Postgres)

Track every recommendation and actual execution for performance analysis.

```sql
CREATE TABLE recommendations (
    id SERIAL PRIMARY KEY,
    date DATE NOT NULL,
    symbol VARCHAR(10) NOT NULL,
    action_type VARCHAR(50) NOT NULL,    -- 'close_early', 'roll', 'new_put', etc.
    strike DECIMAL(10,2),
    expiration DATE,
    premium_target DECIMAL(10,4),
    reasoning TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE executions (
    id SERIAL PRIMARY KEY,
    recommendation_id INT REFERENCES recommendations(id),
    executed BOOLEAN DEFAULT FALSE,
    execution_price DECIMAL(10,4),
    execution_time TIMESTAMP,
    notes TEXT
);

CREATE TABLE daily_snapshots (
    id SERIAL PRIMARY KEY,
    date DATE NOT NULL,
    net_liquidation DECIMAL(12,2),
    daily_theta DECIMAL(10,2),
    portfolio_delta DECIMAL(10,2),
    portfolio_beta_delta DECIMAL(10,2),
    num_positions INT,
    num_signals_fired INT,
    num_trades_executed INT,
    adbe_concentration DECIMAL(5,2),
    capital_efficiency DECIMAL(10,4),  -- theta per dollar deployed
    idle_capital_pct DECIMAL(5,2),
    margin_utilization DECIMAL(5,2),
    regime VARCHAR(20),                  -- 'attack', 'hold', 'defend'
    vix_close DECIMAL(5,2),
    created_at TIMESTAMP DEFAULT NOW()
);
```

---

## Project Structure

```
wheel-copilot/
├── CLAUDE.md                  # Claude Code instructions
├── PLAN.md                    # Sprint plan and status
├── config/
│   ├── watchlist.yaml         # symbols to scan for opportunities
│   ├── trading_params.yaml    # your rule thresholds
│   └── secrets.env            # API keys (gitignored)
├── src/
│   ├── data/
│   │   ├── broker.py          # E*Trade API client (pyetrade)
│   │   ├── auth.py            # OAuth 1.0a token management
│   │   ├── market.py          # yfinance + IV rank calculator
│   │   ├── events.py          # earnings, dividends, macro calendar
│   │   └── models.py          # dataclasses above
│   ├── analysis/
│   │   ├── signals.py         # alpha signal detection (dips, IV, technicals)
│   │   ├── strikes.py         # smart strike selection at technical levels
│   │   ├── sizing.py          # conviction-based position sizing
│   │   ├── scanner.py         # existing position action classification
│   │   ├── opportunities.py   # full pipeline: signals → strikes → sizing → ranking
│   │   └── risk.py            # concentration + Greeks + stress + efficiency
│   ├── reasoning/
│   │   └── briefing.py        # Claude API synthesis
│   ├── delivery/
│   │   ├── telegram_bot.py    # push notification
│   │   └── trade_log.py       # Postgres logging
│   └── main.py                # orchestrator
├── sql/
│   └── schema.sql             # Postgres tables
├── tests/
│   ├── test_scanner.py
│   ├── test_opportunities.py
│   └── fixtures/               # sample portfolio data for testing
└── requirements.txt
```

---

## Config Files

### watchlist.yaml

```yaml
# Core wheel candidates (stocks you'd want to own)
watchlist:
  - AAPL
  - AMZN
  - AVGO
  - GOOG
  - META
  - MSFT
  - NVDA
  - PLTR
  - AMD
  - TSM
  - ADBE
  - CRM
  - NFLX
  - UBER
  - COIN
  # Add/remove based on your conviction list
```

### trading_params.yaml

```yaml
# AGGRESSIVE WHEEL — targeting 40%+ annualized
# This is NOT conservative. You WILL have drawdowns. The edge is
# in recovery speed (close losers fast, reload into dips).
wheel:
  # Put selling
  put_target_delta: -0.30       # aggressive: closer to ATM = fatter premium
  put_min_delta: -0.40          # will go up to 40 delta on HIGH conviction dips
  put_max_delta: -0.15          # won't sell further than this (not worth the capital)
  
  # Call selling  
  call_target_delta: 0.30
  call_min_delta: 0.15
  call_max_delta: 0.40
  
  # DTE window
  min_dte: 21
  max_dte: 45
  sweet_spot_dte: 30            # preferred DTE for new positions
  
  # Premium thresholds
  min_yield_per_trade: 0.015    # 1.5% of capital at risk minimum (aggressive)
  min_annualized_yield: 0.15    # 15% annualized floor — no charity trades
  
  # IV filters
  min_iv_rank: 25               # lower floor — signals can override
  preferred_iv_rank: 50         # flag as "good setup" above this
  iv_rank_override_on_dip: true # if dip signal fires, ignore IV rank floor
  
  # Profit management
  close_at_profit_pct: 0.50     # close when 50% of max profit captured
  close_at_profit_pct_short_dte: 0.80  # tighter target under 14 DTE
  close_and_reload_threshold: 0.40     # close at 40% if a dip signal is pending
  
  # Roll triggers
  roll_when_delta_exceeds: 0.45 # aggressive: let it ride a bit more
  roll_min_net_credit: 0.25     # only roll if you collect at least this credit

signals:
  # Dip detection
  intraday_dip_threshold: -2.5  # % drop to trigger intraday dip signal
  multi_day_pullback_days: 3    # consecutive red days to trigger
  multi_day_pullback_pct: 5.0   # % from recent high to trigger
  
  # Technical
  support_proximity_pct: 3.0    # within 3% of support = signal
  oversold_rsi_threshold: 30    # RSI below this = oversold signal
  
  # IV surface
  iv_rank_spike_threshold: 20   # IV rank jump in 5 days to trigger
  skew_blowout_multiplier: 1.3  # 30% above normal skew = signal
  term_structure_inversion: 1.05 # front/back IV ratio to trigger
  
  # Macro
  vix_fear_threshold: 25        # VIX above this = macro fear signal
  vix_change_threshold: 2.0     # VIX daily point change to trigger
  
  # Earnings IV crush
  earnings_iv_crush_window_min: 5   # days before earnings (minimum)
  earnings_iv_crush_window_max: 15  # days before earnings (maximum)
  earnings_iv_crush_min_iv_rank: 65 # IV rank required for crush play

sizing:
  # Conviction-based position sizing (% of NLV)
  high_conviction_pct: 0.04     # 4% per trade on HIGH signal
  medium_conviction_pct: 0.02   # 2% per trade on MEDIUM signal
  low_conviction_pct: 0.01      # 1% per trade on LOW signal
  
  # Signal strength thresholds
  high_conviction_strength: 70   # avg signal strength for HIGH
  high_conviction_min_signals: 2 # need 2+ confirming signals for HIGH
  medium_conviction_strength: 50
  
  # Scaling
  margin_cutback_threshold: 0.40 # cut size 50% above this margin util
  max_new_trades_per_day: 5      # don't open more than 5 positions/day

portfolio:
  max_concentration_per_symbol: 0.10    # 10% max per ticker
  max_concentration_per_sector: 0.35    # 35% max per sector
  adbe_target_concentration: 0.20       # specific target for your ADBE position
  max_margin_utilization: 0.55          # aggressive: up to 55% margin
  target_idle_capital: 0.10             # target <10% idle in ATTACK regime
  min_cash_reserve: 0.03                # 3% absolute floor (aggressive)

regime:
  # VIX thresholds for regime classification
  attack_vix_range: [18, 30]            # VIX in this range = ATTACK (sell premium)
  hold_vix_range: [14, 18]              # VIX too low, premiums thin = HOLD
  defend_vix_ceiling: 35                # above this, reduce exposure
  crisis_vix: 40                        # above this, close all short options
  
  # Capital deployment targets by regime
  attack_target_deployed: 0.90          # 90% deployed in ATTACK
  hold_target_deployed: 0.70            # 70% deployed in HOLD
  defend_target_deployed: 0.40          # 40% deployed in DEFEND
```

---

## CLAUDE.md (for Claude Code)

```markdown
# Wheel Copilot

## What this is
A daily morning briefing agent for a wheel options strategy portfolio 
(~25 positions across large-cap tech/semis/AI names). It pulls live 
portfolio data, market context, and events, then uses Claude to 
synthesize actionable recommendations.

## Key files
- `config/trading_params.yaml` — all tunable thresholds
- `config/watchlist.yaml` — symbols to scan
- `src/main.py` — the orchestrator, run this to generate a briefing
- `src/analysis/signals.py` — alpha signal detection (dips, IV, technicals)
- `src/analysis/strikes.py` — smart strike selection at support/resistance
- `src/analysis/sizing.py` — conviction-based position sizing
- `src/analysis/scanner.py` — existing position action classification
- `src/analysis/opportunities.py` — full pipeline: signals → strikes → rank

## Conventions
- Python 3.11+, type hints everywhere
- Dataclasses for all data models (in `src/data/models.py`)
- No pandas unless doing heavy data manipulation — prefer dataclasses
- All dollar amounts as Decimal, not float
- All dates as datetime.date
- Tests use pytest with fixtures in `tests/fixtures/`

## Important context
- The trader runs a covered call + cash-secured put (wheel) strategy
- Broker is E*Trade — use `pyetrade` for API access (OAuth 1.0a)
- E*Trade rate limit: 4 req/s market data, 2 req/s account — add 0.3s sleeps
- E*Trade does NOT provide IV rank — calculate from yfinance historical data
- ADBE is an overweight position from RSUs/ESPP — always flag concentration
- Never recommend naked calls — only covered calls on owned stock
- Position sizing follows fixed-fraction (not full Kelly)
- Earnings dates must be checked before ANY recommendation
- The system recommends, human approves — never auto-execute in Phase 1

## Testing
- Use `tests/fixtures/sample_portfolio.json` for testing without broker API
- Mock broker API calls in tests — never hit live API in CI
```

---

## Implementation Sprints

### Sprint 1 (Weekend 1): Data Pipeline
- [ ] Set up project structure
- [ ] Implement broker API client (E*Trade via pyetrade)
- [ ] Implement market data fetcher (yfinance for IV, prices, chains)
- [ ] Implement events calendar (earnings + dividends)
- [ ] Build sample portfolio fixture for testing without live broker
- [ ] Test: pull full portfolio state and print to console

### Sprint 2 (Weekend 2): Analysis Engine
- [ ] Implement alpha signal detection (dip, IV surface, technical levels)
- [ ] Implement PriceHistory builder (SMAs, RSI, swing points, VWAP)
- [ ] Implement smart strike selection at support/resistance levels
- [ ] Implement conviction-based position sizing
- [ ] Implement position scanner with close-and-reload logic
- [ ] Implement risk/concentration/efficiency calculator
- [ ] Implement full opportunity pipeline with composite scoring
- [ ] Test: run analysis on sample portfolio, verify signal detection
- [ ] Tune trading_params.yaml to match your actual preferences

### Sprint 3 (Weekend 3): Claude Integration + Delivery
- [ ] Implement Claude briefing generator with system prompt
- [ ] Implement Telegram bot delivery
- [ ] Set up Postgres schema and trade logging
- [ ] Implement cron trigger (Lambda or local cron)
- [ ] End-to-end test: full pipeline from data → briefing → Telegram

### Sprint 4 (Weekend 4): Polish + Go Live
- [ ] Run parallel: manual review + copilot briefing for 5 trading days
- [ ] Tune prompts based on briefing quality
- [ ] Add daily snapshot logging for performance tracking
- [ ] Add weekend summary: weekly theta collected, win rate, P&L
- [ ] Deploy to always-on server (EC2 micro or Fly.io)

---

## Broker API Quick Start: E*Trade

### Authentication (OAuth 1.0a)

E*Trade uses OAuth 1.0a. First-time setup requires a browser login. After that,
tokens auto-renew during the trading day (valid until midnight ET).

```python
import pyetrade

# Step 1: Get request token (one-time browser flow)
oauth = pyetrade.ETradeOAuth(CONSUMER_KEY, CONSUMER_SECRET)
authorize_url = oauth.get_request_token()
# → Open authorize_url in browser, log in, get verifier code

# Step 2: Exchange for access token
tokens = oauth.get_access_token(verifier_code)
# → Save tokens["oauth_token"] and tokens["oauth_token_secret"]

# Step 3: Create authenticated clients
accounts_client = pyetrade.ETradeAccounts(
    CONSUMER_KEY, CONSUMER_SECRET,
    tokens["oauth_token"], tokens["oauth_token_secret"],
    dev=False  # True for sandbox
)

market_client = pyetrade.ETradeMarket(
    CONSUMER_KEY, CONSUMER_SECRET,
    tokens["oauth_token"], tokens["oauth_token_secret"],
    dev=False
)

order_client = pyetrade.ETradeOrder(
    CONSUMER_KEY, CONSUMER_SECRET,
    tokens["oauth_token"], tokens["oauth_token_secret"],
    dev=False
)
```

**Token refresh strategy for cron jobs:**

```python
import pyetrade
import json
from pathlib import Path

TOKEN_FILE = Path("config/.etrade_tokens.json")

def get_authenticated_clients():
    """
    Load saved tokens and create clients.
    E*Trade access tokens last until midnight ET.
    If expired, the cron job logs an error and you re-auth manually.
    For Phase 4, you can automate renewal with selenium.
    """
    if TOKEN_FILE.exists():
        saved = json.loads(TOKEN_FILE.read_text())
        try:
            # Test if tokens still work
            client = pyetrade.ETradeAccounts(
                CONSUMER_KEY, CONSUMER_SECRET,
                saved["oauth_token"], saved["oauth_token_secret"],
                dev=False
            )
            client.list_accounts()  # will throw if expired
            return build_clients(saved)
        except Exception:
            raise RuntimeError(
                "E*Trade tokens expired. Run `python src/data/auth.py` to re-authenticate."
            )
    else:
        raise FileNotFoundError("No saved tokens. Run auth flow first.")
```

### Fetching Portfolio Positions

```python
def get_portfolio(accounts_client) -> list[dict]:
    """
    Pull all positions across all E*Trade accounts.
    Returns raw position data for each account.
    """
    accounts = accounts_client.list_accounts()
    all_positions = []
    
    for account in accounts["AccountListResponse"]["Accounts"]["Account"]:
        account_id_key = account["accountIdKey"]
        
        portfolio = accounts_client.get_account_portfolio(account_id_key)
        
        if portfolio and "PortfolioResponse" in portfolio:
            for acct_portfolio in portfolio["PortfolioResponse"]["AccountPortfolio"]:
                for position in acct_portfolio.get("Position", []):
                    all_positions.append({
                        "account_id": account_id_key,
                        "symbol": position["symbolDescription"],
                        "product_type": position["Product"]["securityType"],
                        # "EQ" = equity, "OPTN" = option
                        "quantity": position["quantity"],
                        "cost_basis": position.get("totalCost", 0),
                        "market_value": position["marketValue"],
                        "current_price": position["Quick"]["lastTrade"],
                        "pnl_pct": position.get("pctOfPortfolio", 0),
                        
                        # Option-specific fields
                        "strike_price": position["Product"].get("strikePrice"),
                        "expiry_date": position["Product"].get("expiryYear", "") 
                            + "-" + str(position["Product"].get("expiryMonth", "")).zfill(2)
                            + "-" + str(position["Product"].get("expiryDay", "")).zfill(2),
                        "option_type": position["Product"].get("callPut"),
                        # "CALL" or "PUT"
                    })
    
    return all_positions
```

### Fetching Option Chains with Greeks

```python
def get_option_chain_with_greeks(
    market_client,
    symbol: str,
    expiry_year: str,
    expiry_month: int,
    strike_near: float,
    num_strikes: int = 10
) -> list[dict]:
    """
    Pull option chain from E*Trade. Greeks (delta, gamma, theta, vega, IV)
    are included in the OptionGreeks object for each contract.
    """
    chain = market_client.get_option_chains(
        symbol,
        expiry_year=expiry_year,
        expiry_month=expiry_month,
        strike_price_near=strike_near,
        no_of_strikes=num_strikes,
        option_category="STANDARD",
        chain_type="CALLPUT",  # both calls and puts
        price_type="AALL"      # all price types
    )
    
    contracts = []
    
    if chain and "OptionChainResponse" in chain:
        for pair in chain["OptionChainResponse"]["OptionPair"]:
            for side in ["Call", "Put"]:
                if side in pair:
                    opt = pair[side]
                    greeks = opt.get("OptionGreeks", {})
                    
                    contracts.append({
                        "symbol": symbol,
                        "option_type": side.upper(),
                        "strike": opt["strikePrice"],
                        "expiry": opt.get("quoteDetail", ""),
                        "bid": opt["bid"],
                        "ask": opt["ask"],
                        "last": opt["lastPrice"],
                        "volume": opt["volume"],
                        "open_interest": opt["openInterest"],
                        "in_the_money": opt["inTheMoney"] == "y",
                        
                        # Greeks from E*Trade
                        "delta": greeks.get("delta", 0),
                        "gamma": greeks.get("gamma", 0),
                        "theta": greeks.get("theta", 0),
                        "vega": greeks.get("vega", 0),
                        "rho": greeks.get("rho", 0),
                        "iv": greeks.get("iv", 0),
                    })
    
    return contracts
```

### Fetching Quotes (for underlying prices)

```python
def get_quotes(market_client, symbols: list[str]) -> dict:
    """
    Pull real-time quotes for a list of symbols.
    E*Trade allows up to 25 symbols per request.
    """
    quotes = {}
    
    # E*Trade caps at 25 symbols per call
    for batch in [symbols[i:i+25] for i in range(0, len(symbols), 25)]:
        response = market_client.get_quote(
            batch,
            detail_flag="ALL"  # includes fundamentals + intraday data
        )
        
        if response and "QuoteResponse" in response:
            for q in response["QuoteResponse"]["QuoteData"]:
                sym = q["Product"]["symbol"]
                all_data = q.get("All", {})
                
                quotes[sym] = {
                    "price": all_data.get("lastTrade"),
                    "change_pct": all_data.get("changeClose"),
                    "volume": all_data.get("totalVolume"),
                    "high_52w": all_data.get("high52"),
                    "low_52w": all_data.get("low52"),
                    "pe_ratio": all_data.get("pe"),
                    "dividend_yield": all_data.get("dividend"),
                    "ex_dividend_date": all_data.get("exDividendDate"),
                    "earnings_date": all_data.get("nextEarningDate"),
                    # Note: earnings date from quotes is sometimes 
                    # unreliable — cross-check with yfinance
                }
    
    return quotes
```

### Calculating IV Rank (not provided by E*Trade)

```python
import yfinance as yf
import numpy as np
from datetime import datetime, timedelta

def calculate_iv_rank(symbol: str, current_iv: float, lookback_days: int = 252) -> dict:
    """
    IV Rank and IV Percentile from historical options data.
    
    IV Rank = (current_iv - 52w_low) / (52w_high - 52w_low) * 100
    IV Percentile = % of days in lookback where IV was below current
    
    Since historical IV isn't directly available from E*Trade,
    we approximate using yfinance ATM implied vol from the chain,
    or use the VIX-correlated approach for broad approximation.
    """
    ticker = yf.Ticker(symbol)
    
    # Approach: pull historical close prices, calculate realized vol
    # as a proxy for where IV "should" be, then compare to current IV
    hist = ticker.history(period="1y")
    
    if hist.empty:
        return {"iv_rank": 50.0, "iv_percentile": 50.0}  # fallback
    
    # Calculate rolling 30-day realized vol (annualized)
    returns = np.log(hist["Close"] / hist["Close"].shift(1))
    rolling_rv = returns.rolling(window=30).std() * np.sqrt(252) * 100
    rolling_rv = rolling_rv.dropna()
    
    if len(rolling_rv) < 30:
        return {"iv_rank": 50.0, "iv_percentile": 50.0}
    
    rv_min = rolling_rv.min()
    rv_max = rolling_rv.max()
    
    # IV Rank: where does current IV sit in the annual range?
    if rv_max - rv_min > 0:
        iv_rank = (current_iv * 100 - rv_min) / (rv_max - rv_min) * 100
    else:
        iv_rank = 50.0
    
    # IV Percentile: what % of days had lower vol?
    iv_percentile = (rolling_rv < current_iv * 100).sum() / len(rolling_rv) * 100
    
    return {
        "iv_rank": round(max(0, min(100, iv_rank)), 1),
        "iv_percentile": round(max(0, min(100, iv_percentile)), 1),
        "hv_30d": round(rolling_rv.iloc[-1], 2),
        "iv_30d": round(current_iv * 100, 2),
        "iv_hv_spread": round(current_iv * 100 - rolling_rv.iloc[-1], 2)
    }
```

### Placing Orders (Phase 4 — preview only for now)

```python
def preview_option_order(
    order_client,
    account_id_key: str,
    symbol: str,
    option_symbol: str,
    action: str,           # "SELL_OPEN", "BUY_CLOSE", "SELL_CLOSE", "BUY_OPEN"
    quantity: int,
    price_type: str,       # "LIMIT", "MARKET"
    limit_price: float = None,
    order_term: str = "GOOD_FOR_DAY"
) -> dict:
    """
    Preview an order WITHOUT executing. Returns estimated fills and fees.
    Use this in Phase 1-3 for the briefing. Phase 4 swaps preview → place.
    """
    order_payload = {
        "orderType": "EQ",  # even for options, E*Trade uses EQ
        "clientOrderId": f"wc-{datetime.now().strftime('%Y%m%d%H%M%S')}",
        "Order": [{
            "allOrNone": "false",
            "priceType": price_type,
            "orderTerm": order_term,
            "marketSession": "REGULAR",
            "stopPrice": "",
            "limitPrice": str(limit_price) if limit_price else "",
            "Instrument": [{
                "Product": {
                    "securityType": "OPTN",
                    "symbol": option_symbol
                },
                "orderAction": action,
                "orderedQuantity": quantity,
                "quantityType": "QUANTITY"
            }]
        }]
    }
    
    # Preview only — does NOT execute
    preview = order_client.preview_equity_order(
        account_id_key=account_id_key,
        order=order_payload
    )
    
    return preview
```

### E*Trade API Gotchas

1. **OAuth tokens expire at midnight ET.** Your cron job runs at 8am, so
   tokens from yesterday's manual auth may still work if you authenticated
   after midnight. For reliability, authenticate once at market open (or
   automate with selenium hitting the E*Trade login page).

2. **Rate limits:** E*Trade allows 4 requests/second for market data, 
   2 requests/second for account data. With ~25 positions, you'll need
   to batch and pace your calls. Build in a 0.3s sleep between requests.

3. **Option symbols:** E*Trade uses its own format internally but the 
   `Product` object contains `strikePrice`, `expiryYear`, `expiryMonth`,
   `expiryDay`, and `callPut` as separate fields. You'll need to 
   reconstruct OCC-standard symbols if cross-referencing with yfinance.

4. **Sandbox vs Production:** Use `dev=True` in pyetrade constructors for
   sandbox testing. Sandbox data is fake but the API shape is identical.
   Get sandbox keys at developer.etrade.com, production keys require
   a separate approval (usually 1-2 business days).

5. **Greeks quality:** E*Trade computes Greeks server-side using their own
   pricing model. Quality is good for ATM/near-the-money options but can
   be stale for far OTM. For your wheel strikes (~25-30 delta), they're
   reliable enough.

---

## Hitting 40%+ Annualized: Strategy Upgrades

Standard wheel on large caps tops out around 20-25%. To hit 40%+, you need to 
stack multiple edges and be willing to accept more variance. Here's what changes:

### Strategy 1: Aggressive Weekly Puts on Dips (target: 1-2% per week)

```python
class WeeklyDipPlay:
    """
    When a high-conviction dip signal fires, sell WEEKLY puts (5-7 DTE)
    instead of 30 DTE monthlies. The math:
    
    - 30 DTE put at -0.30 delta: ~2% yield, 12x/year = 24% annualized
    - 7 DTE put at -0.30 delta: ~0.8% yield, 52x/year = 41% annualized
    
    Weeklies have faster theta decay (Charm) and let you compound faster.
    The tradeoff: less time to be right. Only use on HIGH conviction dips
    where you'd happily take assignment.
    """
    min_dte: int = 5
    max_dte: int = 10
    required_conviction: str = "high"
    required_signals: int = 2
    target_delta: float = -0.30
    max_portfolio_pct_in_weeklies: float = 0.30  # cap at 30% of NLV
```

### Strategy 2: Strangles on High-IV Names (target: 3-5% per cycle)

```python
class StranglePlay:
    """
    On names with IV rank >70, sell BOTH a put and a call.
    Collects premium on both sides. Works when IV is overpricing movement.
    
    Example: NVDA IV rank 78, stock at $100
    - Sell May 90P @ $3.50 (delta -0.25)
    - Sell May 112C @ $2.80 (delta 0.22)
    - Total premium: $6.30 on ~$21,200 capital = 3.0%
    
    Risk: stock moves past either strike. But the combined premium
    gives you a wider breakeven range. Only on stocks you'd own.
    The call side MUST be covered by shares or this becomes naked.
    """
    min_iv_rank: int = 65
    put_delta: float = -0.25
    call_delta: float = 0.25
    min_combined_yield: float = 0.025  # 2.5% combined
    # CRITICAL: call side requires 100 shares owned per contract
    requires_shares: bool = True
```

### Strategy 3: Earnings IV Crush (target: 5-15% per event)

```python
class EarningsIVCrush:
    """
    Sell premium 5-10 days before earnings when IV is inflated.
    Close the day before the report — capture the IV decay, dodge the gap.
    
    The edge: IV typically rises 20-40% into earnings, then crushes 30-60%
    the day after. You're selling the RUNUP, not holding through the event.
    
    Advanced version: sell a strangle 7 days before earnings, close 1 day
    before. You're delta-neutral-ish and pure theta/vega.
    
    Example: AMZN earnings Apr 24
    - Apr 14: sell May-02 strangle (8 DTE post-earnings expiry)
    - Apr 23: close both legs. IV has been rising for 9 days, your 
      short vega has been losing BUT theta has been paying you more.
    - Net: typically 5-15% on capital if IV ran up enough.
    
    This is the highest-yield play but requires discipline to close
    BEFORE the event. Never hold through.
    """
    entry_window_days_before: tuple = (5, 15)
    min_iv_rank: int = 60
    exit_days_before_earnings: int = 1  # ALWAYS close day before
    prefer_strangle: bool = True  # both sides if shares available
    max_portfolio_pct: float = 0.15  # max 15% of NLV in crush plays
```

### Strategy 4: Put Spread on Lower-Priced Names (target: 15-30% per trade)

```python
class PutSpread:
    """
    For names where cash-secured puts tie up too much capital,
    use defined-risk put spreads instead.
    
    Example: PLTR at $24
    - Sell PLTR May 22P @ $1.20
    - Buy PLTR May 20P @ $0.55
    - Net credit: $0.65 on $2.00 width = 32.5% max return
    
    Pro: Capital efficient. $200 max risk per spread vs $2,200 for CSP.
    Con: Max loss is 100% of capital at risk (vs CSP where you own stock).
    
    Use for: mid-conviction plays where you want exposure but don't
    want to tie up capital on a $20-50 stock.
    """
    max_spread_width: float = 5.0     # $5 max between strikes
    min_credit_pct: float = 0.25      # 25% of spread width minimum
    target_short_delta: float = -0.30
```

### Strategy 5: Dividend Capture + Covered Call (target: 8-12% annualized bonus)

```python
class DividendCapture:
    """
    Get assigned on a put right before ex-div date. Collect dividend.
    Immediately sell a call against the shares.
    
    Example: MSFT ex-div May 15, $0.75/share
    - Sell MSFT May-09 390P (1 week before ex-div)
    - Get assigned at $390. Own shares.
    - Collect $0.75 dividend on May 15.
    - Sell MSFT May-30 400C on May 15 for $3.50.
    - Total income: put premium + dividend + call premium
    
    Stacks 3 income streams on one position.
    """
    target_assignment_before_exdiv: bool = True
    min_dividend_yield_annualized: float = 0.01  # 1% dividend yield floor
```

### Blended Target Math

```
Strategy allocation for 40%+ target:

30% capital → Standard wheel (monthlies)     → 20-25% return
20% capital → Weekly dip puts                → 35-50% return  
15% capital → Strangles on high-IV names     → 30-40% return
15% capital → Earnings IV crush cycles       → 40-60% return
10% capital → Put spreads on smaller names   → 25-40% return
10% capital → Dividend capture combos        → 15-25% return

Blended: ~35-45% annualized at target allocation
With compounding and aggressive redeployment: 40-50%

This assumes:
- ~70% win rate across all strategies
- Average 1-2% yield per trade cycle
- 15-20 cycles per position per year (mix of weekly + monthly)
- Losses capped at 1-2x the premium collected per losing trade
- No catastrophic drawdown (managed by regime shifts + stops)
```

---

## Scout Agent: Social Intelligence Pipeline

The Scout Agent continuously monitors external sources for new position ideas
and catalysts, analyzes them, and feeds qualified opportunities into the
morning briefing pipeline.

### Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                     SCOUT AGENT                                   │
│                  (runs every 2 hours)                              │
│                                                                    │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌──────────┐  │
│  │ Twitter/ │ │ Discord │ │ YouTube │ │  Reddit │ │  News    │  │
│  │ X API   │ │ Bot     │ │ RSS     │ │  API    │ │  Feeds   │  │
│  └────┬────┘ └────┬────┘ └────┬────┘ └────┬────┘ └────┬─────┘  │
│       │           │           │           │            │         │
│       ▼           ▼           ▼           ▼            ▼         │
│  ┌─────────────────────────────────────────────────────────┐     │
│  │              RAW MENTION AGGREGATOR                      │     │
│  │  Deduplicate, normalize tickers, extract context         │     │
│  └──────────────────────┬──────────────────────────────────┘     │
│                         │                                         │
│                         ▼                                         │
│  ┌─────────────────────────────────────────────────────────┐     │
│  │           CLAUDE ANALYSIS LAYER                          │     │
│  │                                                          │     │
│  │  For each ticker with sufficient buzz:                   │     │
│  │  1. Sentiment classification (bullish/bearish/neutral)   │     │
│  │  2. Catalyst extraction (what's driving the buzz?)       │     │
│  │  3. Credibility scoring (who's talking? track record?)   │     │
│  │  4. Novelty check (is this new info or echo chamber?)    │     │
│  │  5. Wheel-fit assessment (good premium seller candidate?)│     │
│  └──────────────────────┬──────────────────────────────────┘     │
│                         │                                         │
│                         ▼                                         │
│  ┌─────────────────────────────────────────────────────────┐     │
│  │          QUANTITATIVE VALIDATION                         │     │
│  │                                                          │     │
│  │  Pull IV rank, options chain, technicals, fundamentals   │     │
│  │  Run through existing alpha signal detection             │     │
│  │  Score opportunity with full analysis engine              │     │
│  └──────────────────────┬──────────────────────────────────┘     │
│                         │                                         │
│                         ▼                                         │
│  ┌─────────────────────────────────────────────────────────┐     │
│  │          QUALIFIED OPPORTUNITY                            │     │
│  │  → Feeds into morning briefing as "SCOUT PICKS"          │     │
│  │  → Or pushes intraday Telegram alert if HIGH conviction  │     │
│  └─────────────────────────────────────────────────────────┘     │
└──────────────────────────────────────────────────────────────────┘
```

### Data Sources

```python
@dataclass
class ScoutSource:
    name: str
    source_type: str
    credibility_weight: float  # 0-1, how much we trust this source
    update_frequency: str      # how often to poll


SCOUT_SOURCES = [
    # === Twitter/X ===
    # FinTwit is the fastest signal. These accounts move markets.
    ScoutSource("unusual_whales", "twitter", 0.8, "15min"),
    ScoutSource("GoldmanSachs", "twitter", 0.9, "1hr"),
    ScoutSource("DeItaone", "twitter", 0.85, "15min"),     # Walter Bloomberg
    ScoutSource("zabornamern", "twitter", 0.7, "30min"),    # Unusual options flow
    ScoutSource("OptionsHawk", "twitter", 0.75, "30min"),
    # Add your own trusted follows here
    
    # === Discord ===
    # Options flow rooms, earnings plays, unusual activity channels
    ScoutSource("thetagang_discord", "discord", 0.6, "1hr"),
    ScoutSource("options_flow_room", "discord", 0.65, "30min"),
    # Specific server/channel IDs in config
    
    # === Reddit ===
    ScoutSource("r/wallstreetbets", "reddit", 0.3, "2hr"),   # low trust, high signal
    ScoutSource("r/options", "reddit", 0.5, "2hr"),
    ScoutSource("r/thetagang", "reddit", 0.6, "2hr"),        # wheel-specific
    
    # === YouTube ===
    # Earnings previews, sector analysis, macro calls
    ScoutSource("tastylive", "youtube", 0.7, "4hr"),
    ScoutSource("InTheMoney_Adam", "youtube", 0.6, "4hr"),
    # Parse video titles + descriptions for ticker mentions
    
    # === News Feeds ===
    ScoutSource("benzinga", "news_api", 0.8, "15min"),
    ScoutSource("seeking_alpha", "rss", 0.5, "1hr"),
    ScoutSource("marketwatch", "rss", 0.7, "30min"),
    ScoutSource("unusual_options_flow", "api", 0.85, "15min"),
    # Benzinga Pro API gives real-time unusual options activity
]
```

### Raw Mention Aggregator

```python
@dataclass
class RawMention:
    ticker: str
    source: str
    source_type: str
    timestamp: datetime
    text: str                    # raw content
    author: str
    author_followers: int | None # for credibility weighting
    engagement: int | None       # likes, retweets, upvotes
    url: str


def aggregate_mentions(
    sources: list[ScoutSource],
    lookback_hours: int = 6
) -> dict[str, list[RawMention]]:
    """
    Pull all mentions from all sources, normalize tickers,
    group by symbol. Returns {symbol: [mentions]}.
    """
    all_mentions = []
    
    for source in sources:
        if source.source_type == "twitter":
            mentions = pull_twitter_mentions(source, lookback_hours)
        elif source.source_type == "discord":
            mentions = pull_discord_mentions(source, lookback_hours)
        elif source.source_type == "reddit":
            mentions = pull_reddit_mentions(source, lookback_hours)
        elif source.source_type == "youtube":
            mentions = pull_youtube_mentions(source, lookback_hours)
        elif source.source_type in ("news_api", "rss"):
            mentions = pull_news_mentions(source, lookback_hours)
        
        all_mentions.extend(mentions)
    
    # Normalize: "$NVDA", "Nvidia", "NVDA" → "NVDA"
    for m in all_mentions:
        m.ticker = normalize_ticker(m.ticker)
    
    # Group by symbol, filter out noise (need 2+ mentions from different sources)
    grouped = defaultdict(list)
    for m in all_mentions:
        grouped[m.ticker].append(m)
    
    # Buzz threshold: need mentions from 2+ different sources
    qualified = {
        symbol: mentions 
        for symbol, mentions in grouped.items()
        if len(set(m.source for m in mentions)) >= 2
        or any(m.engagement and m.engagement > 500 for m in mentions)
    }
    
    return qualified


def pull_twitter_mentions(source: ScoutSource, lookback_hours: int) -> list[RawMention]:
    """
    Use Twitter/X API v2 or a scraping service.
    Search for: cashtags ($NVDA), option flow mentions, 
    unusual activity alerts from flow accounts.
    """
    # Option A: Twitter API v2 (requires developer account)
    # Option B: Apify Twitter scraper (paid, no API needed)
    # Option C: Nitter RSS feeds (free, less reliable)
    pass


def pull_reddit_mentions(source: ScoutSource, lookback_hours: int) -> list[RawMention]:
    """
    Use PRAW (Python Reddit API Wrapper).
    Pull hot/new posts from r/options, r/thetagang, r/wallstreetbets.
    Extract tickers using regex: $TICKER or common patterns.
    """
    import praw
    reddit = praw.Reddit(client_id=REDDIT_ID, client_secret=REDDIT_SECRET,
                         user_agent="wheel-copilot")
    
    subreddit = reddit.subreddit(source.name.replace("r/", ""))
    mentions = []
    
    for post in subreddit.new(limit=50):
        tickers = extract_tickers(post.title + " " + post.selftext)
        for ticker in tickers:
            mentions.append(RawMention(
                ticker=ticker,
                source=source.name,
                source_type="reddit",
                timestamp=datetime.fromtimestamp(post.created_utc),
                text=post.title[:500],
                author=post.author.name if post.author else "deleted",
                author_followers=None,
                engagement=post.score,
                url=f"https://reddit.com{post.permalink}"
            ))
    
    return mentions
```

### Claude Analysis Layer

```python
@dataclass
class ScoutAnalysis:
    ticker: str
    buzz_score: float            # 0-100 based on mention volume + engagement
    sentiment: str               # "bullish", "bearish", "neutral", "mixed"
    catalyst: str                # extracted catalyst driving the buzz
    catalyst_type: str           # "earnings", "product_launch", "macro", "flow", 
                                 # "upgrade_downgrade", "insider", "technical"
    credibility_score: float     # 0-100 weighted by source quality
    novelty: str                 # "new_info", "echo_chamber", "old_news"
    wheel_fit: str               # "excellent", "good", "poor"
    wheel_fit_reasoning: str
    recommended_strategy: str    # "sell_put", "strangle", "put_spread", "skip"
    urgency: str                 # "now", "this_week", "watchlist"


def analyze_mentions_with_claude(
    ticker: str,
    mentions: list[RawMention]
) -> ScoutAnalysis:
    """
    Send aggregated mentions to Claude for analysis.
    Claude determines: is this signal or noise?
    """
    
    client = anthropic.Anthropic()
    
    mentions_text = "\n".join([
        f"[{m.source}] {m.timestamp.strftime('%H:%M')} "
        f"({m.engagement or 0} engagement): {m.text[:300]}"
        for m in sorted(mentions, key=lambda x: x.timestamp, reverse=True)[:20]
    ])
    
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system="""You are an options trader analyzing social media buzz to find 
        premium-selling opportunities. You're skeptical of hype but attentive to 
        real catalysts. You specifically look for situations where buzz creates 
        IV inflation you can sell into, or where fear creates dip-buying opportunities.
        
        For each ticker, assess:
        1. SENTIMENT: What's the crowd saying? Bullish/bearish/mixed?
        2. CATALYST: What's actually driving this? Is it real or rumor?
        3. CREDIBILITY: Are credible sources talking, or just retail noise?
        4. NOVELTY: Is this new information or recycled takes?
        5. WHEEL FIT: Can we sell premium on this? Good IV? Liquid options?
           - Excellent: high IV, liquid options, $50+ stock, you'd own it
           - Good: decent IV, liquid enough, reasonable underlying
           - Poor: low IV, illiquid options, penny stock, binary risk
        6. STRATEGY: What's the play? Sell puts into fear? Strangle if IV rich?
        7. URGENCY: Act now (intraday), this week, or just add to watchlist?
        
        Respond in JSON only. No markdown, no preamble.
        """,
        messages=[{"role": "user", "content": f"""
Analyze this social buzz for {ticker}:

{mentions_text}

Return JSON with keys: sentiment, catalyst, catalyst_type, credibility_score (0-100),
novelty, wheel_fit, wheel_fit_reasoning, recommended_strategy, urgency, summary
"""}]
    )
    
    result = json.loads(response.content[0].text.strip())
    
    # Calculate buzz score from mention volume + engagement
    total_engagement = sum(m.engagement or 0 for m in mentions)
    source_diversity = len(set(m.source for m in mentions))
    buzz_score = min(100, len(mentions) * 10 + source_diversity * 15 
                     + min(50, total_engagement / 100))
    
    return ScoutAnalysis(
        ticker=ticker,
        buzz_score=buzz_score,
        sentiment=result["sentiment"],
        catalyst=result["catalyst"],
        catalyst_type=result["catalyst_type"],
        credibility_score=result["credibility_score"],
        novelty=result["novelty"],
        wheel_fit=result["wheel_fit"],
        wheel_fit_reasoning=result["wheel_fit_reasoning"],
        recommended_strategy=result["recommended_strategy"],
        urgency=result["urgency"]
    )
```

### Quantitative Validation + Full Pipeline

```python
@dataclass
class ScoutOpportunity:
    """A fully validated opportunity from the Scout Agent."""
    # Scout analysis
    scout: ScoutAnalysis
    mentions: list[RawMention]
    
    # Quantitative validation (from existing analysis engine)
    market_context: MarketContext
    price_history: PriceHistory
    alpha_signals: list[AlphaSignal]
    
    # Concrete trade recommendation
    sized_opportunity: SizedOpportunity | None
    
    # Scoring
    composite_score: float       # combines buzz + quant + wheel fit
    is_qualified: bool           # passes all filters


def run_scout_pipeline() -> list[ScoutOpportunity]:
    """
    Full Scout Agent pipeline:
    1. Aggregate mentions from all sources
    2. Analyze each buzzing ticker with Claude
    3. Validate quantitatively (IV, chain, technicals)
    4. Run through existing alpha signal + sizing engine
    5. Return qualified opportunities sorted by composite score
    """
    
    # Step 1: Aggregate
    mentions_by_ticker = aggregate_mentions(SCOUT_SOURCES, lookback_hours=6)
    
    scout_opportunities = []
    
    for ticker, mentions in mentions_by_ticker.items():
        # Step 2: Claude analysis
        analysis = analyze_mentions_with_claude(ticker, mentions)
        
        # Quick filter: skip poor wheel fits and bearish noise
        if analysis.wheel_fit == "poor":
            continue
        if analysis.novelty == "old_news" and analysis.buzz_score < 50:
            continue
        
        # Step 3: Pull quant data
        try:
            mkt = get_market_context(ticker)
            hist = get_price_history(ticker)
            chain = get_options_chain(ticker)
        except Exception:
            continue  # ticker might not have options or be too illiquid
        
        # Step 4: Run through alpha signals
        dip_signals = detect_dip_signals(ticker, mkt, hist)
        iv_signals = detect_iv_surface_signals(ticker, mkt, chain)
        all_signals = dip_signals + iv_signals
        
        # Step 5: Size the opportunity if signals exist
        sized = None
        if all_signals or analysis.credibility_score > 60:
            smart_strikes = find_smart_strikes(
                ticker, chain, hist, 
                analysis.recommended_strategy or "sell_put",
                get_trading_params()
            )
            if smart_strikes:
                opp = Opportunity(
                    symbol=ticker,
                    trade_type=analysis.recommended_strategy or "sell_put",
                    strike=smart_strikes[0].strike,
                    expiration=chain.get_expiry_near_dte(30),
                    premium=smart_strikes[0].premium,
                    yield_on_capital=smart_strikes[0].yield_on_capital,
                    annualized_yield=smart_strikes[0].annualized_yield,
                    iv_rank=mkt.iv_rank,
                    delta=smart_strikes[0].delta,
                    smart_strike=smart_strikes[0],
                    signals=all_signals
                )
                sized = size_position(opp, all_signals, get_portfolio(), get_trading_params())
        
        # Composite score
        quant_score = (sum(s.strength for s in all_signals) / len(all_signals) 
                      if all_signals else 20)
        composite = (
            analysis.buzz_score * 0.20 +
            analysis.credibility_score * 0.25 +
            quant_score * 0.35 +
            (sized.annualized_yield * 200 if sized else 0) * 0.20
        )
        
        scout_opportunities.append(ScoutOpportunity(
            scout=analysis,
            mentions=mentions,
            market_context=mkt,
            price_history=hist,
            alpha_signals=all_signals,
            sized_opportunity=sized,
            composite_score=composite,
            is_qualified=sized is not None and composite > 50
        ))
    
    return sorted(scout_opportunities, key=lambda s: s.composite_score, reverse=True)
```

### Intraday Alert (for urgent Scout picks)

```python
async def check_and_alert_scout():
    """
    Run every 2 hours during market hours.
    If a HIGH urgency, HIGH conviction Scout pick fires,
    push an intraday Telegram alert — don't wait for morning briefing.
    """
    scout_picks = run_scout_pipeline()
    
    urgent = [s for s in scout_picks 
              if s.is_qualified 
              and s.scout.urgency == "now"
              and s.composite_score > 70]
    
    for pick in urgent[:2]:  # max 2 intraday alerts
        msg = f"""
🔔 SCOUT ALERT: {pick.scout.ticker}

CATALYST: {pick.scout.catalyst}
SENTIMENT: {pick.scout.sentiment} | BUZZ: {pick.scout.buzz_score:.0f}/100
SOURCES: {len(pick.mentions)} mentions from {len(set(m.source for m in pick.mentions))} sources

PLAY: {pick.sized_opportunity.trade_type.upper()}
  {pick.sized_opportunity.contracts}x {pick.sized_opportunity.strike}P @ ${pick.sized_opportunity.premium:.2f}
  Yield: {pick.sized_opportunity.yield_on_capital:.1%} | {pick.sized_opportunity.annualized_yield:.0%} ann.
  Conviction: {pick.sized_opportunity.conviction.upper()}

IV Rank: {pick.market_context.iv_rank:.0f} | RSI: {pick.price_history.rsi_14:.0f}
Signals: {', '.join(s.signal_type.value for s in pick.alpha_signals)}

⚡ This is time-sensitive. Review and act within 30 minutes.
"""
        await send_telegram(msg)
```

### Scout Config (scout_sources.yaml)

```yaml
scout:
  # How often to run the full pipeline
  run_frequency_hours: 2
  market_hours_only: true
  
  # Buzz thresholds
  min_mentions_for_analysis: 2        # need 2+ mentions
  min_source_diversity: 2              # from 2+ different sources
  min_buzz_score_for_qualification: 40
  
  # Claude analysis
  max_tickers_per_run: 15              # don't burn API credits on noise
  min_credibility_for_sizing: 50       # need 50+ credibility to size a trade
  
  # Alert thresholds
  intraday_alert_min_composite: 70     # only alert on strong picks
  max_intraday_alerts_per_day: 4       # don't spam yourself
  
  # Source weights (override defaults per source)
  source_overrides:
    unusual_whales:
      credibility_weight: 0.85
      update_frequency: "10min"        # options flow is time-sensitive
    
    r/wallstreetbets:
      credibility_weight: 0.25         # contrarian indicator more than signal
      min_engagement_for_inclusion: 100 # only high-upvote posts

  # Ticker filters
  excluded_tickers:                     # never scout these
    - SPY     # already in your watchlist
    - QQQ
    - ADBE    # you have enough ADBE exposure
  
  min_market_cap: 5_000_000_000        # $5B minimum (need liquid options)
  min_option_volume: 1000              # daily option volume floor
  min_price: 15                        # need reasonable contract sizes
```

### Updated Project Structure

```
wheel-copilot/
├── CLAUDE.md
├── PLAN.md
├── config/
│   ├── watchlist.yaml
│   ├── trading_params.yaml
│   ├── scout_sources.yaml
│   ├── greeks_targets.yaml      # NEW: portfolio-level Greeks limits
│   └── secrets.env
├── src/
│   ├── data/
│   │   ├── broker.py
│   │   ├── auth.py
│   │   ├── market.py
│   │   ├── events.py
│   │   └── models.py
│   ├── analysis/
│   │   ├── signals.py
│   │   ├── strikes.py
│   │   ├── sizing.py
│   │   ├── scanner.py
│   │   ├── opportunities.py
│   │   ├── risk.py
│   │   ├── strategies.py
│   │   ├── correlation.py       # NEW: correlation clusters + hedging
│   │   ├── greeks_guard.py      # NEW: portfolio Greeks pre-trade check
│   │   ├── loss_mgmt.py         # NEW: loss rules + roll/close decisions
│   │   └── drawdown.py          # NEW: drawdown decomposition + diagnosis
│   ├── backtest/                 # NEW: Historical validation
│   │   ├── engine.py            # core backtester
│   │   ├── data_loader.py       # historical prices + IV
│   │   ├── option_pricer.py     # Black-Scholes approximation
│   │   ├── signal_backtest.py   # per-signal validation
│   │   ├── strategy_backtest.py # full strategy simulation
│   │   └── reports.py           # backtest result formatting
│   ├── learning/                 # NEW: Self-tuning feedback loop
│   │   ├── loop.py              # weekly/monthly review engine
│   │   ├── retuner.py           # parameter adjustment logic
│   │   └── audit.py             # adjustment logging + history
│   ├── scout/
│   │   ├── aggregator.py
│   │   ├── twitter.py
│   │   ├── reddit.py
│   │   ├── discord_bot.py
│   │   ├── youtube.py
│   │   ├── news.py
│   │   ├── analyzer.py
│   │   ├── validator.py
│   │   └── alerts.py
│   ├── execution/                # NEW: Execution optimization
│   │   ├── repricer.py          # intraday re-pricing engine
│   │   ├── conditional.py       # conditional order logic
│   │   ├── timing.py            # execution window optimization
│   │   └── quality.py           # slippage + fee tracking
│   ├── reasoning/
│   │   └── briefing.py
│   ├── delivery/
│   │   ├── telegram_bot.py
│   │   └── trade_log.py
│   └── main.py
├── sql/
│   └── schema.sql
├── tests/
│   ├── test_signals.py
│   ├── test_scanner.py
│   ├── test_backtest.py
│   ├── test_loss_mgmt.py
│   ├── test_greeks_guard.py
│   └── fixtures/
└── requirements.txt
```

### Updated Sprint Plan

```
Sprint 1 (Weekend 1): Data Pipeline (E*Trade + market data)
Sprint 2 (Weekend 2): Analysis Engine (signals, strikes, sizing)
Sprint 3 (Weekend 3): Claude Integration + Delivery
Sprint 4 (Weekend 4): Go Live (morning briefing)
Sprint 5 (Weekend 5): Strategy Upgrades (weeklies, strangles, spreads)
Sprint 6 (Weekend 6): Scout Agent - Reddit + News feeds
Sprint 7 (Weekend 7): Scout Agent - Twitter + Discord + YouTube
Sprint 8 (Weekend 8): Scout Agent - Intraday alerts + full integration
```

---

## Portfolio Allocation Framework: The Three-Engine Model

The portfolio is three engines, each with a different job, return profile,
and risk tolerance. The advisor layer manages the balance between them.

### Engine Definitions

```python
@dataclass
class EngineAllocation:
    """Current state of each portfolio engine."""
    
    # Engine 1: Core Holdings — long-term equity appreciation
    core_holdings: dict[str, CorePosition]  # symbol → position
    core_target_pct: float = 0.45           # 45% of NLV
    core_actual_pct: float = 0.0
    core_expected_return: float = 0.18      # 15-20% from appreciation + dividends
    
    # Engine 2: Active Wheel — premium selling machine
    wheel_positions: dict[str, list[Position]]  # symbol → active options
    wheel_target_pct: float = 0.45
    wheel_actual_pct: float = 0.0
    wheel_expected_return: float = 0.45     # 40-50% on deployed capital
    
    # Engine 3: Dry Powder — cash for opportunistic deployment
    cash_available: float = 0.0
    powder_target_pct: float = 0.10
    powder_actual_pct: float = 0.0


@dataclass
class CorePosition:
    """A long-term holding in Engine 1."""
    symbol: str
    shares: int
    cost_basis: float
    current_value: float
    holding_period_days: int
    is_ltcg_eligible: bool          # held >12 months
    days_until_ltcg: int | None     # days until long-term treatment
    conviction: str                 # "highest", "high", "medium"
    thesis: str                     # why you own this long-term
    
    # Coverage
    total_shares: int
    covered_shares: int             # shares with calls sold against them
    uncovered_shares: int           # shares held for pure appreciation
    coverage_ratio: float           # covered / total (target: 30-50% for E1)
    
    # Dividend
    annual_dividend: float | None
    next_ex_div: date | None
    dividend_yield: float | None
```

### Buy vs Sell-Put Decision Framework

```python
class EntryDecision(Enum):
    BUY_SHARES = "buy_shares"           # direct purchase for Engine 1
    SELL_PUT = "sell_put"               # premium entry for Engine 2
    SPLIT_ENTRY = "split_entry"         # buy some shares + sell puts
    SELL_PUT_TARGETING_ASSIGNMENT = "put_to_own"  # sell ITM-ish put to GET assigned


def decide_entry_method(
    symbol: str,
    mkt: MarketContext,
    hist: PriceHistory,
    signals: list[AlphaSignal],
    conviction_profile: dict      # from your watchlist config
) -> tuple[EntryDecision, str]:
    """
    The critical decision: when to BUY vs when to SELL PUTS.
    
    BUY SHARES when:
    - Long-term compounder (highest conviction, 3-5 year thesis)
    - AND stock just dropped hard (>5% in a week)
    - AND you want the FULL upside from the recovery bounce
    - Because: put premium is small compared to catching a 15% bounce
    
    SELL PUTS when:
    - IV is elevated (IV rank >50)
    - AND you want a DISCOUNT entry (get paid to wait for lower price)
    - AND the stock is range-bound or slowly grinding up
    - Because: if not assigned, you still collected premium income
    
    SPLIT ENTRY when:
    - High conviction + big dip + elevated IV
    - Best of both worlds: buy some now for appreciation, sell puts for income
    - This is the power move on a fat dip day
    
    SELL PUT TO OWN when:
    - You want assignment (sell ATM or slightly ITM put)
    - Stock just cratered and you want a large position fast
    - The put premium effectively reduces your cost basis further
    """
    
    is_long_term_compounder = conviction_profile.get("conviction") == "highest"
    big_dip = hist.drawdown_from_n_day_high(5) > 5.0
    iv_elevated = mkt.iv_rank > 50
    signal_count = len(signals)
    
    # Scenario 1: Compounder on a big dip — BUY NOW
    if is_long_term_compounder and big_dip and not iv_elevated:
        return (EntryDecision.BUY_SHARES,
                f"{symbol} is a core compounder down {hist.drawdown_from_n_day_high(5):.1f}% "
                f"— buy shares outright for Engine 1. Don't leave upside on the table "
                f"waiting for put assignment.")
    
    # Scenario 2: Compounder on a big dip WITH elevated IV — SPLIT
    if is_long_term_compounder and big_dip and iv_elevated:
        return (EntryDecision.SPLIT_ENTRY,
                f"{symbol} down {hist.drawdown_from_n_day_high(5):.1f}% AND IV rank "
                f"{mkt.iv_rank:.0f}. Split: buy 60% shares for Engine 1, sell puts on "
                f"40% for Engine 2 to collect the fear premium.")
    
    # Scenario 3: Good name, IV rich, no major dip — SELL PUTS
    if iv_elevated and signal_count >= 1:
        return (EntryDecision.SELL_PUT,
                f"{symbol} IV rank {mkt.iv_rank:.0f} with {signal_count} signals. "
                f"Sell puts for Engine 2 — get paid to wait for a better entry.")
    
    # Scenario 4: Name you want to own NOW — PUT TO OWN
    if is_long_term_compounder and big_dip and signal_count >= 2:
        return (EntryDecision.SELL_PUT_TARGETING_ASSIGNMENT,
                f"{symbol} high conviction + fat dip + multiple signals. "
                f"Sell ATM put to maximize premium and GET assigned.")
    
    # Default
    return (EntryDecision.SELL_PUT, "Standard wheel entry for Engine 2.")
```

### Assignment Routing

```python
def route_assignment(
    symbol: str,
    shares_assigned: int,
    cost_basis: float,
    conviction_profile: dict,
    current_engine1: dict,
    current_engine2: dict,
    allocation: EngineAllocation
) -> dict:
    """
    When you get assigned on a short put, decide how to split
    the shares between Engine 1 (hold) and Engine 2 (sell calls).
    """
    
    conviction = conviction_profile.get("conviction", "medium")
    
    if conviction == "highest":
        # Keep most shares uncovered for appreciation
        engine1_shares = int(shares_assigned * 0.70)  # 70% uncovered
        engine2_shares = shares_assigned - engine1_shares  # 30% covered
        call_delta = 0.15  # far OTM calls — don't want to get called away
        reasoning = (f"HIGHEST conviction — routing 70% to Engine 1 (uncovered), "
                    f"30% to Engine 2 with 0.15 delta calls (unlikely assignment).")
    
    elif conviction == "high":
        engine1_shares = int(shares_assigned * 0.50)
        engine2_shares = shares_assigned - engine1_shares
        call_delta = 0.20
        reasoning = (f"HIGH conviction — 50/50 split. Engine 2 calls at 0.20 delta.")
    
    else:
        # Pure wheel — sell calls on everything
        engine1_shares = 0
        engine2_shares = shares_assigned
        call_delta = 0.30
        reasoning = (f"MEDIUM conviction — full Engine 2. Sell 0.30 delta calls, "
                    f"happy to get called away and redeploy capital.")
    
    return {
        "engine1_shares": engine1_shares,
        "engine2_shares": engine2_shares,
        "sell_calls_against": engine2_shares,
        "call_delta": call_delta,
        "reasoning": reasoning
    }
```

### Dynamic Rebalancing

```python
@dataclass
class RebalanceAction:
    action: str           # "shift_to_core", "shift_to_wheel", "build_powder", "deploy_powder"
    amount: float         # dollar amount to move
    from_engine: str
    to_engine: str
    reasoning: str
    urgency: str          # "now", "this_week", "next_rebalance"


def check_rebalancing(
    allocation: EngineAllocation,
    regime: str,
    portfolio: PortfolioState
) -> list[RebalanceAction]:
    """
    Check if engines have drifted from targets and recommend shifts.
    Regime changes also trigger rebalancing.
    """
    actions = []
    nlv = portfolio.net_liquidation
    
    # Regime-adjusted targets
    regime_targets = {
        "attack": {"core": 0.50, "wheel": 0.45, "powder": 0.05},
        "hold":   {"core": 0.50, "wheel": 0.35, "powder": 0.15},
        "defend": {"core": 0.40, "wheel": 0.30, "powder": 0.30},
        "crisis": {"core": 0.35, "wheel": 0.15, "powder": 0.50},
    }
    
    targets = regime_targets.get(regime, regime_targets["hold"])
    
    # Check each engine for drift >5%
    core_drift = allocation.core_actual_pct - targets["core"]
    wheel_drift = allocation.wheel_actual_pct - targets["wheel"]
    powder_drift = allocation.powder_actual_pct - targets["powder"]
    
    # Core overweight → skim profits to wheel or powder
    if core_drift > 0.05:
        skim_amount = core_drift * nlv
        actions.append(RebalanceAction(
            action="shift_to_wheel",
            amount=skim_amount,
            from_engine="core",
            to_engine="wheel",
            reasoning=f"Engine 1 overweight by {core_drift:.1%}. Trim weakest core "
                      f"positions and feed ${skim_amount:,.0f} into Engine 2.",
            urgency="this_week"
        ))
    
    # Wheel outperforming → pull profits into core
    if wheel_drift > 0.05:
        pull_amount = wheel_drift * nlv
        actions.append(RebalanceAction(
            action="shift_to_core",
            amount=pull_amount,
            from_engine="wheel",
            to_engine="core",
            reasoning=f"Engine 2 overweight by {wheel_drift:.1%}. Great — wheel is "
                      f"producing. Pull ${pull_amount:,.0f} and buy core positions.",
            urgency="this_week"
        ))
    
    # Powder too low in defend/crisis regime
    if regime in ("defend", "crisis") and powder_drift < -0.10:
        build_amount = abs(powder_drift) * nlv
        actions.append(RebalanceAction(
            action="build_powder",
            amount=build_amount,
            from_engine="wheel",
            to_engine="powder",
            reasoning=f"DEFEND/CRISIS regime but only {allocation.powder_actual_pct:.0%} "
                      f"cash. Close wheel positions to build ${build_amount:,.0f} war chest.",
            urgency="now"
        ))
    
    return actions
```

---

## Financial Advisor Layer

The advisor sits above everything and provides holistic portfolio guidance
that goes beyond individual trade recommendations.

### Tax Optimization

```python
@dataclass
class TaxContext:
    symbol: str
    shares: int
    cost_basis: float
    current_price: float
    unrealized_gain: float
    holding_start_date: date
    holding_period_days: int
    is_ltcg: bool                    # held >365 days
    days_until_ltcg: int | None      # None if already LTCG
    tax_lot_id: str | None           # for specific lot identification
    
    # Estimated tax impact
    stcg_rate: float = 0.37          # short-term = ordinary income (top bracket)
    ltcg_rate: float = 0.20          # long-term capital gains rate
    estimated_tax_if_sold_now: float = 0.0
    estimated_tax_if_ltcg: float = 0.0
    tax_savings_by_waiting: float = 0.0  # stcg_tax - ltcg_tax


def generate_tax_alerts(positions: list[TaxContext]) -> list[str]:
    """
    Flag positions approaching LTCG eligibility or with large tax implications.
    """
    alerts = []
    
    for pos in positions:
        # Approaching LTCG — DON'T sell calls that risk assignment
        if pos.days_until_ltcg and 0 < pos.days_until_ltcg <= 60:
            alerts.append(
                f"⏰ TAX: {pos.symbol} ({pos.shares} shares) reaches LTCG status "
                f"in {pos.days_until_ltcg} days ({pos.holding_start_date + timedelta(days=365):%b %d}). "
                f"DO NOT sell covered calls that risk early assignment. "
                f"Tax savings: ${pos.tax_savings_by_waiting:,.0f}."
            )
        
        # Large unrealized STCG — consider holding
        if not pos.is_ltcg and pos.unrealized_gain > 5000:
            alerts.append(
                f"💰 TAX: {pos.symbol} has ${pos.unrealized_gain:,.0f} unrealized STCG. "
                f"Selling now costs ~${pos.estimated_tax_if_sold_now:,.0f} in taxes. "
                f"Waiting for LTCG saves ${pos.tax_savings_by_waiting:,.0f}."
            )
        
        # Tax-loss harvesting opportunity
        if pos.unrealized_gain < -2000:
            alerts.append(
                f"📉 TAX HARVEST: {pos.symbol} has ${abs(pos.unrealized_gain):,.0f} unrealized loss. "
                f"Consider selling to harvest the loss, wait 31 days (wash sale), "
                f"then re-enter via put sale."
            )
    
    return alerts
```

### ADBE Concentration Management Plan

```python
@dataclass
class ConcentrationPlan:
    """Standing plan for managing ADBE RSU/ESPP concentration."""
    
    current_adbe_pct: float
    target_adbe_pct: float = 0.15       # target 15% max
    
    # ESPP vesting schedule
    next_espp_vest_date: date | None
    next_espp_vest_value: float | None
    
    # RSU vesting schedule
    next_rsu_vest_date: date | None
    next_rsu_vest_value: float | None
    
    # Standing sell plan
    shares_to_sell_per_quarter: int = 0
    sell_trigger: str = "vest"          # sell on vest, or spread over quarter
    
    # Redeployment plan
    redeploy_to_engine1_pct: float = 0.40   # 40% into core holdings
    redeploy_to_engine2_pct: float = 0.50   # 50% into wheel buying power
    redeploy_to_powder_pct: float = 0.10    # 10% to dry powder
    
    # Preferred Engine 1 targets for redeployment
    redeploy_targets: list[str] = None  # e.g., ["AMZN", "AVGO", "MSFT"]


def generate_adbe_plan(
    adbe_position: CorePosition,
    allocation: EngineAllocation,
    upcoming_vests: list[dict]
) -> str:
    """Generate quarterly ADBE management plan."""
    
    excess_pct = adbe_position.current_value / allocation.core_actual_pct - 0.15
    
    if excess_pct <= 0:
        return "ADBE concentration within target. No action needed."
    
    excess_value = excess_pct * allocation.core_actual_pct
    
    plan = f"""
ADBE MANAGEMENT PLAN (Quarterly)
Current: {adbe_position.current_value / allocation.core_actual_pct:.1%} of NLV
Target:  15%
Excess:  ${excess_value:,.0f}

STANDING ORDERS:
1. On each ESPP/RSU vest: sell 100% of new shares immediately
2. Additionally sell {adbe_position.total_shares // 8} existing shares per quarter
3. Redeploy proceeds:
   - 40% → buy {', '.join(adbe_position.redeploy_targets or ['AMZN', 'AVGO'])} (Engine 1)
   - 50% → Engine 2 buying power (wheel capital)
   - 10% → Dry powder

UPCOMING VESTS:
"""
    for vest in upcoming_vests:
        plan += f"  {vest['date']}: ~${vest['value']:,.0f} ({vest['type']})\n"
    
    return plan
```

### Performance Attribution

```python
@dataclass
class PerformanceAttribution:
    """Track which strategies and signals are actually making money."""
    
    period: str                          # "mtd", "qtd", "ytd"
    
    # By engine
    engine1_return: float                # % return on Engine 1 capital
    engine2_return: float                # % return on Engine 2 capital
    blended_return: float                # portfolio-level return
    
    # By strategy (Engine 2 breakdown)
    monthly_put_return: float            # standard 30 DTE puts
    weekly_put_return: float             # weekly dip puts
    strangle_return: float
    earnings_crush_return: float
    put_spread_return: float
    dividend_capture_return: float
    
    # By signal type — which signals actually predict winners?
    signal_performance: dict[str, dict]  # signal_type → {trades, win_rate, avg_return}
    
    # By conviction level
    high_conviction_return: float
    medium_conviction_return: float
    low_conviction_return: float
    
    # Scout performance
    scout_pick_return: float             # return on Scout-sourced trades
    scout_win_rate: float
    scout_vs_watchlist: float            # are Scout picks better than your watchlist?


def generate_weekly_review(
    trades: list[dict],
    snapshots: list[dict]
) -> PerformanceAttribution:
    """
    Weekly performance review. Critical for tuning.
    
    Key questions answered:
    - Are high-conviction trades actually outperforming low-conviction?
    - Which signal types have the best hit rate?
    - Are Scout picks adding alpha or just noise?
    - Which strategies are carrying the portfolio?
    - What's your actual realized Sharpe ratio?
    """
    # ... implementation groups trades by strategy, signal, conviction
    # and computes win rates + returns for each bucket
    pass
```

### Psychological Guardrails (Tilt Detection)

```python
@dataclass
class TiltDetector:
    """
    Detect when you're overtrading, revenge trading, or deviating 
    from the system. A pro has rules AND the discipline to follow them.
    """
    
    # Overtrading detection
    trades_today: int = 0
    trades_this_week: int = 0
    max_trades_per_day: int = 5
    max_trades_per_week: int = 15
    
    # Revenge trading detection
    consecutive_losses: int = 0
    loss_streak_threshold: int = 3      # alert after 3 losses in a row
    
    # System override detection
    trades_against_signal: int = 0      # trades taken WITHOUT system signal
    manual_overrides_this_week: int = 0
    
    # Drawdown psychology
    peak_nlv: float = 0.0
    current_drawdown_pct: float = 0.0
    max_drawdown_before_pause: float = 0.15  # pause after 15% drawdown


def check_tilt(detector: TiltDetector) -> list[str]:
    alerts = []
    
    if detector.trades_today >= detector.max_trades_per_day:
        alerts.append(
            "🛑 TILT ALERT: You've hit your daily trade limit. "
            "Step away. No more trades today."
        )
    
    if detector.consecutive_losses >= detector.loss_streak_threshold:
        alerts.append(
            f"🛑 TILT ALERT: {detector.consecutive_losses} consecutive losses. "
            f"This is when revenge trading happens. Take tomorrow off."
        )
    
    if detector.current_drawdown_pct >= detector.max_drawdown_before_pause:
        alerts.append(
            f"🛑 DRAWDOWN LIMIT: Portfolio down {detector.current_drawdown_pct:.1%} "
            f"from peak. System shifting to DEFEND regime automatically. "
            f"Close 50% of Engine 2 positions this week."
        )
    
    if detector.manual_overrides_this_week >= 3:
        alerts.append(
            "⚠️ DISCIPLINE: You've overridden the system 3+ times this week. "
            "Review whether your manual trades outperformed the system's picks."
        )
    
    return alerts
```

### Execution Optimization

```python
@dataclass
class ExecutionRules:
    """
    How to actually place orders for best fills.
    The difference between market orders and smart limit orders 
    is 2-5% of premium collected per trade — it compounds.
    """
    
    # Entry timing
    preferred_entry_window: str = "10:00-10:30 ET"  
    # Why: the opening 30 minutes are noisy. Spreads are wide.
    # 10:00-10:30 is when the "morning dip" often bottoms and 
    # spreads tighten. This is the sweet spot for selling puts.
    
    avoid_first_15_min: bool = True     # never trade 9:30-9:45
    avoid_last_15_min: bool = True      # never trade 3:45-4:00
    avoid_fomc_day_before_2pm: bool = True  # wait for the announcement
    
    # Order types
    default_order_type: str = "LIMIT"   # NEVER market orders on options
    limit_price_strategy: str = "mid_minus_penny"
    # Start at mid price, then walk down 1 penny every 30 seconds
    # until filled. Patient limit orders save 5-10c per contract.
    
    max_spread_pct: float = 0.05        # skip if bid-ask spread >5% of mid
    # Wide spreads mean illiquid options — your fill will be bad
    
    # Multi-leg orders
    use_combo_orders: bool = True       # for rolls and strangles, submit as single order
    # Combo orders get better fills than legging in separately


def calculate_smart_limit(bid: float, ask: float, direction: str) -> float:
    """
    Calculate optimal limit price. Don't pay the ask. Don't lowball the bid.
    """
    mid = (bid + ask) / 2
    spread = ask - bid
    
    if direction == "sell":
        # Start at mid, willing to go to mid - 1 penny
        # Market makers will often fill at mid on liquid names
        return round(mid - 0.01, 2)
    else:  # buying to close
        return round(mid + 0.01, 2)
```

### Bloodbath Protocol

The system needs three modes for catastrophic events, each triggered differently:

1. BROAD MARKET CRASH (SPY -5%+ in a day, VIX >35)
2. SECTOR REPRICING (software/tech names down 30-70%, AI disruption narrative)
3. EMPLOYER-SPECIFIC CRISIS (ADBE down 20%+ in a week — you work there)

```python
class BloodbathProtocol:
    """
    Comprehensive crisis management system.
    Three phases: PROTECT → STABILIZE → ATTACK THE RECOVERY.
    
    The critical insight: the worst day for your portfolio is the 
    best day of the year for premium selling. The system must 
    survive the crash AND position you to profit from the recovery.
    """

    # =========================================================
    # PHASE 0: INSTANT REGIME DETECTION (runs every 60 seconds)
    # =========================================================
    
    class InstantRegimeMonitor:
        """
        VIX and market drop detection that runs INDEPENDENTLY of 
        the 5x daily analysis schedule. This is a dedicated loop 
        that only does one thing: detect regime changes instantly.
        
        The gap in the original design: analysis cycles at 8am/10:30/1pm 
        could miss a crash at 11am. This fixes that.
        """
        
        CHECK_INTERVAL = 60  # seconds — checks VIX and SPY every minute
        
        # Escalating thresholds
        THRESHOLDS = {
            "elevated": {"vix": 25, "spy_drop": -0.02},   # attention
            "severe":   {"vix": 30, "spy_drop": -0.03},   # regime shift
            "crisis":   {"vix": 35, "spy_drop": -0.05},   # emergency
            "extreme":  {"vix": 45, "spy_drop": -0.08},   # all hands
        }
        
        async def monitor_loop(self):
            """Always-on loop during market hours. 60-second cycle."""
            while is_market_open():
                vix = await get_vix_price()  # single API call
                spy = await get_spy_intraday_change()  # single API call
                
                level = self.classify(vix, spy)
                
                if level != self.current_level:
                    await self.trigger_regime_change(level, vix, spy)
                    self.current_level = level
                
                await asyncio.sleep(self.CHECK_INTERVAL)
        
        async def trigger_regime_change(self, level: str, vix: float, spy_drop: float):
            """
            Immediate actions when regime changes. 
            Does NOT wait for the next scheduled analysis cycle.
            """
            if level == "crisis" or level == "extreme":
                # IMMEDIATE Telegram push
                await send_telegram(f"""
🚨 BLOODBATH ALERT — {level.upper()}

VIX: {vix:.1f} | SPY: {spy_drop:+.1%} today

AUTOMATIC ACTIONS (executing now):
1. Closing ALL weekly positions (DTE < 10)
2. Canceling ALL pending orders
3. Regime → CRISIS — all new trades blocked
4. Margin stress check running...

AWAITING YOUR INPUT:
5. Roll remaining monthlies out for time? [YES] [NO]
6. Deploy dry powder into dip? [NOT YET] [SMALL] [AGGRESSIVE]

Do NOT panic. The system is protecting you.
Review in 15 minutes when the dust settles.
""")
                # Execute protective actions immediately
                await close_all_weeklies()
                await cancel_pending_orders()
                await set_regime("crisis")
    
    # =========================================================
    # PHASE 1: PROTECT (first 0-2 hours of bloodbath)
    # =========================================================
    
    class CrisisProtection:
        """
        Immediate defensive actions. Speed matters.
        The goal: survive the first 2 hours without catastrophic loss.
        """
        
        # Crisis spread handling
        async def close_with_crisis_spreads(self, positions: list):
            """
            During bloodbath, bid-ask spreads blow out 5-20x.
            Normal limit orders won't fill.
            
            Strategy: 
            1. Try limit at mid-price for 30 seconds
            2. Walk the price toward the bid by $0.25 every 15 seconds
            3. After 2 minutes, use market order (accept the slippage)
            
            Losing $2 to slippage > losing $10 waiting for a fill
            """
            for pos in positions:
                spread = pos.ask - pos.bid
                spread_pct = spread / pos.mid_price if pos.mid_price else 1
                
                if spread_pct > 0.15:
                    # Crisis spread — use aggressive fill strategy
                    await self.aggressive_close(pos, max_slippage_pct=0.20)
                else:
                    # Normal spread — standard limit
                    await self.limit_close(pos, price=pos.mid_price + 0.01)
        
        # Margin stress projection
        async def project_margin_stress(self, portfolio, current_spy_drop: float):
            """
            Ask: if the market drops another 3% from HERE, do I get a margin call?
            If yes, preemptively close the weakest positions NOW.
            
            Broker forced liquidation ALWAYS happens at the worst price.
            Closing voluntarily at -8% is better than being force-liquidated at -12%.
            """
            projected_drop = current_spy_drop - 0.03  # another 3% worse
            
            projected_nlv = portfolio.net_liquidation * (1 + projected_drop)
            projected_margin_req = sum(
                estimate_crisis_margin(pos, projected_drop) 
                for pos in portfolio.positions
            )
            
            margin_ratio = projected_margin_req / projected_nlv
            
            if margin_ratio > 0.85:
                # Approaching margin call territory
                # Identify positions to close (weakest conviction first)
                positions_to_close = sorted(
                    portfolio.positions,
                    key=lambda p: (p.conviction_score, p.profit_pct),  # close lowest first
                )
                
                # Close enough to bring margin to 60%
                await self.preemptive_margin_reduce(positions_to_close, target_ratio=0.60)
                
                return f"""
🛑 MARGIN STRESS: At {projected_drop:+.1%} scenario, margin ratio hits {margin_ratio:.0%}.
Preemptively closing {len(positions_to_close)} weakest positions to prevent forced liquidation.
Better to close on our terms than the broker's.
"""
            return None
        
        # Crisis correlation override
        def apply_crisis_correlations(self, portfolio):
            """
            During crashes, backward-looking correlation is meaningless.
            Everything moves together. Override to 0.95 for all tech/semi.
            """
            crisis_correlation = 0.95
            
            tech_positions = [p for p in portfolio.positions 
                            if p.sector in ("Technology", "Semiconductors", "Software")]
            
            effective_positions = len(portfolio.positions) - len(tech_positions) + 1
            # All tech = 1 effective position during crisis
            
            return effective_positions
    
    # =========================================================
    # PHASE 2: SECTOR REPRICING DETECTION
    # =========================================================
    
    class SectorRepricingDetector:
        """
        Detect when the bloodbath is sector-specific, not broad.
        "AI eats software" is different from "market crashes."
        
        In a sector repricing:
        - NVDA might be UP while CRM is down 40%
        - The correct response is to SELL CRM and BUY NVDA, not hide in cash
        - Your existing positions split into winners and losers within the SAME portfolio
        """
        
        def detect_narrative_divergence(
            self, 
            portfolio: PortfolioState,
            market_data: dict
        ) -> dict:
            """
            Check if positions in the portfolio are diverging significantly.
            Group into "winners" and "losers" based on today's price action.
            """
            winners = []
            losers = []
            
            for pos in portfolio.stock_positions:
                change = market_data[pos.symbol]["change_1d"]
                if change > 0.01:  # up >1%
                    winners.append({"symbol": pos.symbol, "change": change})
                elif change < -0.05:  # down >5%
                    losers.append({"symbol": pos.symbol, "change": change})
            
            divergence = (
                np.mean([w["change"] for w in winners]) - 
                np.mean([l["change"] for l in losers])
            ) if winners and losers else 0
            
            is_sector_repricing = divergence > 0.08  # 8%+ spread between winners/losers
            
            if is_sector_repricing:
                return {
                    "type": "sector_repricing",
                    "narrative": self.identify_narrative(winners, losers),
                    "winners": winners,
                    "losers": losers,
                    "divergence": divergence,
                    "recommendation": self.generate_repricing_plan(winners, losers, portfolio)
                }
            return {"type": "broad_selloff"}
        
        def identify_narrative(self, winners, losers) -> str:
            """
            Use Claude to identify the narrative driving the divergence.
            This determines whether losers are cheap or dying.
            """
            # Claude API call to classify the event
            # "AI disruption repricing SaaS" vs "rate hike fear" vs "earnings miss cluster"
            pass
        
        def generate_repricing_plan(self, winners, losers, portfolio) -> str:
            """
            Sector repricing playbook:
            1. DON'T sell winners to cover losers
            2. For each loser: is the business fundamentally impaired, or is this panic?
            3. If panic on a good business → SELL PUTS (Engine 2) + BUY SHARES (Engine 1)
            4. If fundamentally impaired → CLOSE and redeploy into winners
            5. KEY: the premium on losers during repricing is 3-5x normal — best selling opportunity
            """
            plan = "━━ SECTOR REPRICING DETECTED ━━\n\n"
            
            plan += "WINNERS (do not sell these to cover losses):\n"
            for w in winners:
                plan += f"  ✅ {w['symbol']} {w['change']:+.1%} — HOLD\n"
            
            plan += "\nLOSERS (evaluate each one):\n"
            for l in losers:
                plan += f"  🔴 {l['symbol']} {l['change']:+.1%} — "
                plan += "FUNDAMENTAL CHECK NEEDED:\n"
                plan += f"     Is {l['symbol']}'s business dying or is this panic?\n"
                plan += f"     If panic → sell puts (premiums are 3-5x normal)\n"
                plan += f"     If dying → close position, redeploy to winners\n"
            
            return plan
    
    # =========================================================
    # PHASE 3: EMPLOYER-SPECIFIC CRISIS (ADBE)
    # =========================================================
    
    class EmployerCrisisProtocol:
        """
        Special handling for Adobe — you work there.
        If ADBE drops 20%+ in a week, the concentration plan's 
        quarterly selling schedule is too slow. Accelerate.
        
        ALSO: if the sector is repricing because "AI kills Adobe,"
        your JOB is at risk alongside your PORTFOLIO. The system 
        must account for correlated income + portfolio risk.
        """
        
        EMPLOYER_TICKER = "ADBE"
        EMERGENCY_DROP_THRESHOLD = -0.20  # 20% in 5 days
        
        def check_employer_crisis(
            self, 
            adbe_price_history: list[float],
            portfolio: PortfolioState,
            nlv: float
        ) -> str | None:
            """
            If ADBE drops 20%+ in 5 trading days, override the 
            quarterly sell plan with emergency acceleration.
            """
            if len(adbe_price_history) < 5:
                return None
            
            five_day_return = (adbe_price_history[-1] - adbe_price_history[-5]) / adbe_price_history[-5]
            
            if five_day_return <= self.EMERGENCY_DROP_THRESHOLD:
                adbe_value = sum(
                    p.shares * p.current_price 
                    for p in portfolio.stock_positions 
                    if p.symbol == self.EMPLOYER_TICKER
                )
                adbe_pct = adbe_value / nlv
                
                return f"""
🚨 EMPLOYER CRISIS: ADBE {five_day_return:+.1%} in 5 days

ADBE is {adbe_pct:.0%} of your portfolio (${adbe_value:,.0f}).
You ALSO earn income from Adobe. This is correlated risk.

EMERGENCY OVERRIDE — accelerating sell plan:

IMMEDIATE (today):
  • Sell 50% of ADBE shares above your target concentration (15%)
  • Accept the tax hit — the tax cost of selling at STCG is small 
    compared to another 30% drawdown on a concentrated position
  • This is NOT panic selling — this is risk management for a 
    position that's correlated with your employment income

THIS WEEK:
  • Sell covered calls on ALL remaining ADBE shares (aggressive delta)
  • These calls will either generate income or get you called away
    at a higher price — both outcomes reduce concentration

REDEPLOY:
  • 50% → non-ADBE Engine 1 names (diversification)
  • 50% → Engine 2 buying power (the high-IV environment means 
    premium selling on OTHER names is exceptional right now)

CONSIDER:
  • If Adobe is doing layoffs, you may need MORE liquid cash
  • Ensure emergency reserve is fully funded (6+ months)
  • ADBE vesting schedule may be at risk — don't count unvested RSUs

This is the one scenario where tax efficiency DOES NOT override 
risk management. Sell to protect yourself.
"""
            return None
    
    # =========================================================
    # PHASE 4: RECOVERY ATTACK (hours to days after bloodbath)
    # =========================================================
    
    class RecoveryPlaybook:
        """
        The bloodbath is over. VIX peaked. Breadth is improving.
        NOW you attack — this is the best premium selling and 
        Engine 1 buying opportunity of the year.
        
        Key question: HOW DO YOU KNOW THE BOTTOM IS IN?
        Answer: you don't. But you can detect STABILIZATION signals
        that suggest the worst of the panic is over.
        """
        
        STABILIZATION_SIGNALS = {
            "vix_peak": "VIX made a lower high (peaked and declining)",
            "volume_climax": "Record volume day followed by lower volume",
            "breadth_improvement": "Advance/decline ratio improving from extreme lows",
            "sector_leadership": "At least 3 sectors turning green while others still red",
            "credit_stabilize": "High-yield spreads stopped widening",
            "vix_term_structure": "Term structure returns to contango (front < back)",
        }
        
        def detect_stabilization(self, market_data: dict) -> dict:
            """
            Check for stabilization signals. Need 3+ to trigger recovery mode.
            """
            signals_firing = {}
            
            # VIX peaked — today's VIX is lower than yesterday's
            if market_data["vix"] < market_data["vix_yesterday"]:
                signals_firing["vix_peak"] = True
            
            # Volume climax — yesterday had extreme volume, today is lower
            if (market_data["spy_volume_yesterday"] > market_data["spy_avg_volume"] * 2.5 
                and market_data["spy_volume"] < market_data["spy_volume_yesterday"]):
                signals_firing["volume_climax"] = True
            
            # VIX term structure normalizing
            if market_data.get("vix") and market_data.get("vix3m"):
                if market_data["vix"] < market_data["vix3m"]:  # contango
                    signals_firing["vix_term_structure"] = True
            
            num_signals = len(signals_firing)
            
            if num_signals >= 3:
                return {
                    "mode": "recovery_attack",
                    "confidence": "high" if num_signals >= 4 else "medium",
                    "signals": signals_firing,
                }
            elif num_signals >= 2:
                return {
                    "mode": "recovery_watch",
                    "confidence": "low",
                    "signals": signals_firing,
                }
            else:
                return {"mode": "still_crisis"}
        
        def generate_recovery_plan(
            self, 
            stabilization: dict,
            portfolio: PortfolioState,
            market_data: dict
        ) -> str:
            """
            The recovery attack plan. Deploy dry powder and sell 
            the fattest premiums of the year.
            """
            return f"""
━━ RECOVERY ATTACK PLAN ━━

Stabilization signals: {len(stabilization['signals'])}/6 firing
Confidence: {stabilization['confidence'].upper()}
VIX: {market_data['vix']:.1f} (down from peak)

ENGINE 1 — BUY THE DIPS (long-term):
  Deploy {50 if stabilization['confidence'] == 'high' else 25}% of dry powder.
  Priority: highest-conviction names at deepest discounts.
  These are the entries you'll brag about in 3 years.

ENGINE 2 — HARVEST THE FEAR (premium selling):
  IV rank is 80+ across the board. Premiums are 3-5x normal.
  Sell monthly puts on HIGH conviction names at support levels.
  DO NOT sell weeklies yet — gamma too high during recovery.
  
  Sizing: MEDIUM conviction sizing (2% per trade), not HIGH.
  The bottom might not be in. Scale in over 3-5 days, not all at once.
  
  Best setups: names that dropped on SECTOR FEAR but have intact fundamentals.
  These will recover fastest and your puts expire worthless.

ENGINE 3 — RESERVE:
  Keep {50 if stabilization['confidence'] == 'medium' else 25}% dry powder in reserve.
  If this is a dead cat bounce, you need ammo for the second leg down.

CRITICAL RULES:
  • Scale in over days, not hours. No one catches the exact bottom.
  • Sell monthly puts, not weeklies. Gamma is lethal during recovery.
  • Engine 1 buys at limit orders 5% below current price.
    If they fill, great. If not, the market recovered without you — that's fine.
  • Review daily. If VIX makes a new high, back to CRISIS mode.
"""

    # =========================================================
    # PRE-MARKET SENTINEL (catches overnight bloodbaths)
    # =========================================================
    
    class PreMarketSentinel:
        """
        Markets crash at 3am when Asia sells off or a geopolitical event fires.
        You wake up to -5% futures. The system should already be thinking.
        
        Runs at 6:00 AM, 7:00 AM, and 7:30 AM on trading days.
        Checks futures, VIX futures, and overnight news.
        """
        
        SENTINEL_TIMES = ["06:00", "07:00", "07:30"]
        
        EMERGENCY_THRESHOLDS = {
            "spy_futures_drop": -0.02,     # -2% overnight
            "vix_futures_spike": 5.0,       # +5 points overnight
            "nasdaq_futures_drop": -0.03,   # -3% overnight (tech-heavy)
        }
        
        async def run_sentinel(self):
            """Pre-market check against futures data."""
            spy_futures = await get_futures_price("ES")
            vix_futures = await get_futures_price("VX")
            ndx_futures = await get_futures_price("NQ")
            
            spy_change = (spy_futures - self.spy_prior_close) / self.spy_prior_close
            vix_change = vix_futures - self.vix_prior_close
            ndx_change = (ndx_futures - self.ndx_prior_close) / self.ndx_prior_close
            
            is_emergency = (
                spy_change <= self.EMERGENCY_THRESHOLDS["spy_futures_drop"] or
                vix_change >= self.EMERGENCY_THRESHOLDS["vix_futures_spike"] or
                ndx_change <= self.EMERGENCY_THRESHOLDS["nasdaq_futures_drop"]
            )
            
            if is_emergency:
                await send_telegram(f"""
🚨 PRE-MARKET EMERGENCY — {datetime.now().strftime('%I:%M %p')}

Overnight futures:
  S&P 500: {spy_change:+.1%}
  Nasdaq:  {ndx_change:+.1%}
  VIX:     {vix_futures:.1f} ({vix_change:+.1f})

Your portfolio (estimated overnight impact):
  Open positions: {len(self.portfolio.positions)}
  Estimated P&L: ${self.estimate_overnight_pnl(spy_change):+,.0f}
  Margin concern: {'YES 🛑' if self.margin_at_risk(spy_change) else 'NO ✅'}

The morning briefing at 8am will have full analysis.
But if you need to act NOW:

{'🛑 MARGIN AT RISK — consider closing 2-3 positions at market open' 
 if self.margin_at_risk(spy_change) else 
 '✅ Margin OK — no emergency action needed before open'}

{'⚠️ WEEKLY OPTIONS EXPOSED — close all DTE < 5 at open'
 if any(p.days_to_expiry < 5 for p in self.portfolio.positions) else ''}

[SHOW FULL POSITIONS] [I'LL WAIT FOR 8AM BRIEFING]
""")
```

### Standing Rules (always active, original emergency protocol)

```python
STANDING_RULES = {
    # If any single position loses more than 200% of premium collected, close it
    "max_loss_per_position": "2x premium received (1.5x for weeklies)",
    
    # If portfolio drops 10% in a single day, close all weeklies
    "daily_crash_protocol": "close all positions with DTE < 10",
    
    # If you haven't logged in for 48 hours, the system:
    "absence_protocol": [
        "Close any position within 3 DTE (avoid pin risk)",
        "Do NOT open new positions",
        "Send escalating Telegram alerts every 4 hours",
    ],
    
    # Dead man's switch
    "dead_man_switch": {
        "trigger": "No Telegram interaction for 72 hours",
        "actions": [
            "Close ALL short options (entire Engine 2)",
            "Shift to 100% Engine 3 (cash)",
            "Send alerts to secondary contact (if configured)",
            "System enters hibernation until you respond",
        ]
    },
    
    # If VIX gaps above 40 overnight:
    "vix_circuit_breaker": [
        "Close all positions with DTE < 14",
        "Roll remaining positions out 30+ DTE for time",
        "Do NOT sell new premium until VIX drops below 35",
        "Switch to CRISIS regime allocation",
    ],
    
    # ADBE-specific
    "employer_crisis": {
        "trigger": "ADBE -20% in 5 trading days",
        "action": "Accelerate concentration sell plan to immediate",
        "override": "Tax efficiency does NOT override employer-correlated risk",
    },
}
```

### Correlation & Hedging Monitor

```python
@dataclass
class CorrelationReport:
    """
    Your 25 positions might be "diversified" by name but all correlated
    to the same risk factor (Nasdaq, AI narrative, rate expectations).
    This catches hidden concentration.
    """
    
    # Portfolio beta to major indices
    spy_beta: float          # overall market sensitivity
    qqq_beta: float          # tech/growth sensitivity  
    smh_beta: float          # semiconductor sensitivity
    
    # Correlation cluster analysis
    clusters: list[dict]     # groups of positions that move together
    # e.g., [{"names": ["NVDA", "AMD", "AVGO", "TSM"], "correlation": 0.82}]
    
    effective_positions: int  # diversification-adjusted position count
    # 25 names with 0.80 avg correlation ≈ 5-6 effective positions
    
    # Tail risk hedging
    portfolio_put_cost: float    # cost to buy 5% OTM SPY puts for 30 days
    portfolio_put_cost_pct: float # as % of NLV
    hedge_recommendation: str


def check_hedging_need(
    correlation: CorrelationReport,
    regime: str,
    portfolio: PortfolioState
) -> str | None:
    """
    Recommend portfolio hedges when risk is concentrated.
    """
    
    # If effective positions < 8, you're concentrated regardless of name count
    if correlation.effective_positions < 8:
        return (
            f"⚠️ CORRELATION: You have 25 names but only {correlation.effective_positions} "
            f"effective positions. Your portfolio moves like a leveraged QQQ bet. "
            f"Consider: buy 1x SPY {date.today() + timedelta(days=30)} put at 5% OTM "
            f"for ${correlation.portfolio_put_cost:,.0f} ({correlation.portfolio_put_cost_pct:.2%} of NLV). "
            f"This is insurance, not a trade."
        )
    
    # In DEFEND/CRISIS, always recommend a hedge
    if regime in ("defend", "crisis"):
        return (
            f"🛡️ HEDGE: {regime.upper()} regime active. Buy protective puts: "
            f"SPY {date.today() + timedelta(days=45)} put, 5% OTM, cost "
            f"${correlation.portfolio_put_cost:,.0f}. Covers "
            f"~${portfolio.net_liquidation * 0.05:,.0f} of downside."
        )
    
    return None
```

### Seasonal & Calendar Awareness

```python
@dataclass
class SeasonalContext:
    """
    Certain times of year and certain calendar events systematically
    affect IV and opportunity quality.
    """
    
    # Earnings season density
    is_earnings_season: bool        # True during Jan/Apr/Jul/Oct reporting waves
    earnings_season_week: int       # 1-4 within the season
    # Week 1-2: megacap tech reports → IV elevated across sector
    # Week 3-4: smaller names → opportunities thin out
    
    # FOMC cluster
    next_fomc: date | None
    days_to_fomc: int | None
    # IV tends to rise 3-5 days before FOMC, then crush after
    # Sell premium 5 days before, close day of announcement
    
    # Quadruple witching
    next_opex: date                 # monthly options expiration (3rd Friday)
    days_to_opex: int
    is_quad_witching: bool          # quarterly opex (Mar/Jun/Sep/Dec)
    # Pin risk increases near opex — avoid holding ATM into expiry
    
    # Historical seasonal patterns
    seasonal_bias: str              # "bullish" (Nov-Apr) or "cautious" (May-Oct)
    
    # Year-end considerations
    is_tax_loss_season: bool        # Oct-Dec, losers get dumped
    is_january_effect: bool         # Jan, beaten-down names recover
```

### Slippage & Fee Tracking

```python
@dataclass
class ExecutionQuality:
    """
    Track the hidden costs that eat returns.
    Even 5 cents of slippage per contract × 500 trades/year = $2,500 lost.
    """
    
    # Per trade
    expected_fill: float
    actual_fill: float
    slippage: float                  # actual - expected (negative = worse fill)
    slippage_pct: float              # slippage as % of premium
    
    # Aggregate
    total_slippage_mtd: float
    total_slippage_ytd: float
    avg_slippage_per_trade: float
    worst_slippage_trade: dict       # the one that stung
    
    # Fees
    total_fees_mtd: float
    total_fees_ytd: float
    fees_as_pct_of_premium: float    # target: <2%
    
    # Spread quality
    avg_spread_at_entry: float       # how wide were spreads on your trades
    pct_filled_at_mid: float         # % of orders filled at mid or better
```

---

## Backtesting Framework

Before trading a dollar with any signal or strategy, validate it against history.
This turns opinions into numbers.

```python
@dataclass
class BacktestConfig:
    """Configuration for backtesting a signal or strategy."""
    start_date: date                   # typically 2-3 years back
    end_date: date
    initial_capital: float = 100_000
    signals_to_test: list[str] = None  # specific signal types, or "all"
    strategies_to_test: list[str] = None
    slippage_assumption: float = 0.03  # 3 cents per contract
    commission_per_contract: float = 0.65


@dataclass
class BacktestResult:
    """Output of a backtest run for one signal or strategy."""
    signal_or_strategy: str
    
    # Core metrics
    total_trades: int
    win_rate: float                    # % of trades profitable
    avg_return_per_trade: float        # % return on capital at risk
    avg_winner: float                  # avg return on winning trades
    avg_loser: float                   # avg return on losing trades
    profit_factor: float               # gross_wins / gross_losses
    
    # Risk metrics
    max_drawdown: float                # worst peak-to-trough
    max_drawdown_duration_days: int    # how long to recover
    sharpe_ratio: float                # risk-adjusted return (annualized)
    sortino_ratio: float               # downside-only risk adjustment
    calmar_ratio: float                # annual return / max drawdown
    
    # Timing
    avg_days_in_trade: float
    best_month: float
    worst_month: float
    pct_profitable_months: float
    
    # Signal-specific (if testing signals)
    signal_hit_rate: float             # % of times signal predicted correct direction
    avg_signal_strength_winners: float # were stronger signals better?
    avg_signal_strength_losers: float
    optimal_signal_threshold: float    # strength cutoff that maximizes Sharpe
    
    # Comparison
    vs_buy_and_hold_spy: float         # excess return over SPY buy-and-hold
    vs_vanilla_wheel: float            # excess return over simple 30-DTE delta-0.25 wheel


class Backtester:
    """
    Historical backtest engine. Uses yfinance for price data and 
    reconstructed option chains for premium estimation.
    
    KEY INSIGHT: You can't get historical options chain data for free.
    Options: (1) Use CBOE datashop ($), (2) Use OptionsDX ($30/mo),
    (3) Approximate premiums using Black-Scholes with historical IV.
    Option 3 is good enough for signal validation — you're testing 
    whether your ENTRY TIMING is right, not exact premium amounts.
    """
    
    def __init__(self, config: BacktestConfig):
        self.config = config
        self.historical_prices = {}    # symbol → DataFrame
        self.historical_iv = {}        # symbol → DataFrame
    
    def run_signal_backtest(self, signal_type: str) -> BacktestResult:
        """
        For each day in the backtest period:
        1. Compute all indicators (SMAs, RSI, IV rank, etc.)
        2. Check if the signal would have fired
        3. If fired, simulate the trade (entry at signal, exit at rules)
        4. Record outcome
        
        The goal: does this signal predict profitable put-selling entries?
        """
        trades = []
        
        for day in self.trading_days:
            for symbol in self.watchlist:
                hist = self.get_history_as_of(symbol, day)
                mkt = self.reconstruct_market_context(symbol, day)
                
                signals = detect_signals_for_backtest(signal_type, symbol, mkt, hist)
                
                for signal in signals:
                    # Simulate the trade
                    entry = self.simulate_put_entry(symbol, day, signal)
                    if entry:
                        exit_result = self.simulate_trade_lifecycle(entry)
                        trades.append(exit_result)
        
        return self.compute_metrics(trades)
    
    def run_strategy_backtest(self, strategy: str) -> BacktestResult:
        """
        Full strategy simulation including position management,
        sizing, and capital allocation across the portfolio.
        """
        portfolio = SimulatedPortfolio(self.config.initial_capital)
        
        for day in self.trading_days:
            # Run the full analysis pipeline as of this day
            opportunities = self.simulate_daily_pipeline(day, portfolio)
            
            # Execute the top opportunities (respecting sizing limits)
            for opp in opportunities[:5]:  # max 5 new trades per day
                portfolio.open_position(opp)
            
            # Manage existing positions
            for pos in portfolio.open_positions:
                action = self.simulate_position_scan(pos, day)
                if action in ("close_early", "close_reload"):
                    portfolio.close_position(pos, day)
                elif action == "roll":
                    portfolio.roll_position(pos, day)
            
            # Record daily snapshot
            portfolio.record_snapshot(day)
        
        return self.compute_metrics(portfolio.closed_trades)
    
    def approximate_option_premium(
        self, symbol: str, strike: float, dte: int, 
        spot: float, iv: float, option_type: str = "put"
    ) -> float:
        """
        Black-Scholes approximation for backtesting.
        Not exact, but good enough to validate signal timing.
        """
        from scipy.stats import norm
        import math
        
        T = dte / 365
        r = 0.05  # risk-free rate assumption
        d1 = (math.log(spot / strike) + (r + iv**2 / 2) * T) / (iv * math.sqrt(T))
        d2 = d1 - iv * math.sqrt(T)
        
        if option_type == "put":
            return strike * math.exp(-r * T) * norm.cdf(-d2) - spot * norm.cdf(-d1)
        else:
            return spot * norm.cdf(d1) - strike * math.exp(-r * T) * norm.cdf(d2)


# Run backtests for all signals and rank them
def backtest_all_signals() -> dict[str, BacktestResult]:
    """
    Run every signal type through 3 years of history.
    Output: which signals actually work and which are noise.
    
    Expected output example:
    
    Signal              Win%  Avg Return  Sharpe  Optimal Threshold
    ──────────────────────────────────────────────────────────────
    oversold_rsi         78%    +2.1%      2.4    RSI < 28
    multi_day_pullback   71%    +1.8%      2.1    3+ days, >6% drop
    macro_fear_spike     74%    +2.5%      1.9    VIX > 27
    iv_rank_spike        65%    +1.6%      1.7    +25 in 5 days
    support_bounce       68%    +1.4%      1.5    within 2% of 200 SMA
    intraday_dip         62%    +1.2%      1.3    >3% drop
    skew_blowout         58%    +1.1%      1.1    >40% above normal
    term_inversion       55%    +0.9%      0.9    >1.08 ratio
    ──────────────────────────────────────────────────────────────
    
    Use these results to:
    1. Set optimal signal thresholds (not guesses)
    2. Weight signals in the composite score by their Sharpe ratio
    3. Kill signals that don't beat vanilla wheel
    4. Focus sizing on the top 4-5 signals
    """
    config = BacktestConfig(
        start_date=date(2023, 1, 1),
        end_date=date(2025, 12, 31)
    )
    
    results = {}
    for signal_type in SignalType:
        bt = Backtester(config)
        results[signal_type.value] = bt.run_signal_backtest(signal_type.value)
    
    return results
```

### Backtest Data Sources

| Data | Source | Cost | Notes |
|---|---|---|---|
| Historical prices | yfinance | Free | Daily OHLCV, reliable |
| Historical IV | OptionsDX or CBOE datashop | $30-100/mo | Needed for accurate premium estimation |
| Reconstructed chains | Black-Scholes approximation | Free | Good enough for signal validation |
| Full chain snapshots | OptionsDX bulk download | $50/mo | For strategy-level backtesting |

---

## Loss Management Rules

The spec was detailed on entries and profit-taking but thin on what to do when trades go wrong. These are the hard rules.

```python
@dataclass
class LossManagementRules:
    """
    Every trade has a plan for what happens if it goes against you.
    BEFORE you enter, you know the exit conditions for BOTH sides.
    """
    
    # === DEFINED MAX LOSS PER TRADE ===
    
    # Short puts: close for a loss when the option price reaches 2x premium received
    # Example: sold put for $3.00 → close if it reaches $6.00
    put_max_loss_multiplier: float = 2.0
    
    # For weekly puts (higher gamma): tighter stop at 1.5x
    weekly_put_max_loss_multiplier: float = 1.5
    
    # Put spreads: let these run to expiry (defined risk)
    # Max loss is the spread width minus credit received
    spread_max_loss: str = "defined_by_spread_width"
    
    # Strangles: close the losing side at 2x premium, keep the winning side
    strangle_leg_max_loss_multiplier: float = 2.0
    
    # === UNDERLYING-BASED STOPS ===
    
    # If the underlying drops 15% below your put strike, close regardless
    # At this point assignment gives you a stock in freefall
    underlying_crash_stop_pct: float = 0.15
    
    # === ROLL OR CLOSE DECISION TREE ===
    # When a put is tested (underlying near/below strike):
    
    # IF the underlying is ABOVE the strike (still OTM):
    #   → DO NOTHING. Let theta work. The option is still winning.
    
    # IF the underlying is WITHIN 2% of strike AND DTE > 21:
    #   → ROLL down and out for a net credit. Lower strike + more time.
    #   → Minimum net credit of $0.25 or don't roll (close instead).
    
    # IF the underlying is WITHIN 2% of strike AND DTE < 21:
    #   → DECISION POINT based on conviction:
    #     HIGH conviction → take assignment, immediately sell calls
    #     LOW conviction → close for a loss, redeploy elsewhere
    
    # IF the underlying is BELOW the strike (ITM):
    #   → IF loss < 2x premium → roll if possible for net credit
    #   → IF loss >= 2x premium → close. Eat the loss. Move on.
    #   → NEVER "hope" a deeply ITM short put recovers. The 
    #     opportunity cost of tied-up capital is worse than the loss.
    
    # === PORTFOLIO-LEVEL LOSS LIMITS ===
    
    # If total realized losses this week exceed 3% of NLV, stop trading for 2 days
    weekly_loss_limit_pct: float = 0.03
    weekly_loss_cooldown_days: int = 2
    
    # If total realized losses this month exceed 8% of NLV, shift to DEFEND regime
    monthly_loss_limit_pct: float = 0.08
    
    # If 3+ trades in a row are losers, reduce position sizes by 50% for next 5 trades
    consecutive_loss_size_reduction: int = 3
    size_reduction_factor: float = 0.50
    size_reduction_trades: int = 5


def evaluate_losing_position(
    pos: Position,
    mkt: MarketContext,
    rules: LossManagementRules
) -> tuple[str, str]:
    """
    Given a position that's underwater, determine the action.
    Returns (action, reasoning).
    """
    
    current_loss_multiple = pos.current_price / pos.entry_price
    
    # Hard stop: 2x premium received
    max_multiplier = (rules.weekly_put_max_loss_multiplier 
                     if pos.days_to_expiry <= 10 
                     else rules.put_max_loss_multiplier)
    
    if current_loss_multiple >= max_multiplier:
        return ("CLOSE_LOSS", 
                f"Option at {current_loss_multiple:.1f}x entry premium. "
                f"Max loss rule ({max_multiplier}x) triggered. Close now.")
    
    # Underlying crash stop
    if pos.position_type == "short_put":
        underlying_drop = (pos.strike - mkt.price) / pos.strike
        if underlying_drop > rules.underlying_crash_stop_pct:
            return ("CLOSE_LOSS",
                    f"Underlying {underlying_drop:.0%} below strike. "
                    f"Crash stop triggered. Do not hold into freefall.")
    
    # ITM but within loss tolerance — can we roll?
    if pos.distance_from_strike_pct < 0 and pos.days_to_expiry > 14:
        # Try to find a roll for net credit
        roll_credit = estimate_roll_credit(pos, mkt)
        if roll_credit >= 0.25:
            return ("ROLL",
                    f"ITM but rollable for ${roll_credit:.2f} net credit. "
                    f"Roll down and out 30 days.")
    
    # Near strike, short DTE — take assignment or close
    if pos.distance_from_strike_pct < 2.0 and pos.days_to_expiry <= 14:
        conviction = get_conviction_score(pos.symbol)
        if conviction == "high":
            return ("TAKE_ASSIGNMENT",
                    f"Near strike, short DTE. HIGH conviction name. "
                    f"Accept assignment, sell calls immediately.")
        else:
            return ("CLOSE_LOSS",
                    f"Near strike, short DTE. LOW conviction. "
                    f"Close the position and redeploy capital.")
    
    return ("HOLD", "Position underwater but within tolerance. Monitor.")
```

---

## Portfolio-Level Greeks Targets

Individual position Greeks aren't enough. The portfolio needs aggregate targets
that constrain total directional exposure, volatility sensitivity, and time decay.

```python
@dataclass
class PortfolioGreeksTargets:
    """
    Target ranges for portfolio-level Greeks by regime.
    If a new trade would push any Greek outside the range, 
    the system blocks or downsizes the trade.
    """
    
    # Beta-weighted delta (SPY-equivalent shares)
    # This is your net market exposure.
    delta_target_attack: tuple = (200, 500)     # moderately bullish
    delta_target_hold: tuple = (100, 300)       # mildly bullish
    delta_target_defend: tuple = (-100, 150)    # near neutral
    delta_target_crisis: tuple = (-200, 50)     # flat to slightly short
    
    # Portfolio theta (daily income from time decay)
    # This is what the portfolio earns each day if nothing moves.
    theta_target_attack: float = 200            # $200+/day in ATTACK
    theta_target_hold: float = 100              # $100+/day minimum
    theta_min_per_dollar_deployed: float = 0.003  # $0.30 theta per $100 deployed
    
    # Portfolio vega (sensitivity to IV changes)
    # You're structurally short vega (selling options). Cap the exposure.
    max_vega_pct_of_nlv: float = 0.02           # max 2% NLV at risk per 1pt IV move
    # Example: $750K NLV → max $15,000 vega loss per 1-point IV increase
    
    # Portfolio gamma (acceleration risk)
    # Gamma gets dangerous with weeklies — the P&L moves fast
    max_weekly_gamma_exposure: float = 0.005     # max 0.5% NLV per $1 underlying move
    
    # Net portfolio beta
    max_portfolio_beta: float = 1.50             # never more than 1.5x SPY
    target_portfolio_beta_attack: float = 1.20   # slightly leveraged bull in ATTACK
    target_portfolio_beta_defend: float = 0.60   # low exposure in DEFEND


def check_greeks_before_trade(
    new_trade: SizedOpportunity,
    portfolio: PortfolioState,
    targets: PortfolioGreeksTargets,
    regime: str
) -> tuple[bool, str]:
    """
    Before opening a new position, check if it would push 
    portfolio Greeks outside acceptable ranges.
    Returns (allowed, reason).
    """
    
    # Projected portfolio delta after trade
    projected_delta = portfolio.portfolio_beta_delta + (
        new_trade.contracts * new_trade.delta * 100
    )
    
    delta_range = getattr(targets, f"delta_target_{regime}")
    if not (delta_range[0] <= projected_delta <= delta_range[1]):
        return (False, 
                f"BLOCKED: This trade would push portfolio delta to "
                f"{projected_delta:.0f}, outside {regime.upper()} range "
                f"({delta_range[0]}-{delta_range[1]}). Either close a "
                f"bullish position first or take a bearish trade to offset.")
    
    # Check vega
    projected_vega_risk = abs(portfolio.portfolio_vega + 
                              new_trade.contracts * new_trade.vega * 100)
    max_vega = portfolio.net_liquidation * targets.max_vega_pct_of_nlv
    if projected_vega_risk > max_vega:
        return (False,
                f"BLOCKED: Portfolio vega would reach ${projected_vega_risk:,.0f} "
                f"(max ${max_vega:,.0f}). You're too exposed to an IV spike. "
                f"Close some short options or buy a hedge first.")
    
    # Check weekly gamma
    if new_trade.days_to_expiry <= 10:
        projected_gamma = abs(new_trade.contracts * new_trade.gamma * 100 
                              * portfolio.net_liquidation)
        max_gamma = portfolio.net_liquidation * targets.max_weekly_gamma_exposure
        if projected_gamma > max_gamma:
            return (False,
                    f"BLOCKED: Weekly gamma exposure too high. "
                    f"Reduce contract count or switch to monthly expiry.")
    
    return (True, "Trade within all Greeks limits.")
```

---

## Learning Loop (Self-Tuning System)

Performance attribution tracks results. The learning loop ACTS on them —
retuning signal weights, source credibility, and strategy allocations.

```python
@dataclass
class LearningLoopConfig:
    """
    Run weekly (Sunday evening) and monthly (end of month).
    Examines past trades and adjusts parameters for the next period.
    """
    weekly_review_day: str = "sunday"
    monthly_review_day: int = 1          # 1st of each month
    min_trades_for_adjustment: int = 20  # need 20+ trades before tuning
    max_adjustment_per_cycle: float = 0.15  # max 15% change per tuning cycle
    # Prevents overreaction to small samples


class LearningLoop:
    """
    Closed-loop feedback system that tunes parameters based on results.
    """
    
    def run_weekly_review(self, trades: list[TradeRecord]) -> list[Adjustment]:
        """
        Weekly review produces parameter adjustments. Claude synthesizes
        the data and recommends specific changes with reasoning.
        """
        adjustments = []
        
        # === 1. RETUNE SIGNAL WEIGHTS ===
        # If RSI oversold has 78% win rate and skew blowout has 52%,
        # increase RSI's weight in the composite score.
        signal_performance = self.compute_signal_performance(trades)
        
        for signal_type, perf in signal_performance.items():
            if perf["trades"] < 5:
                continue  # not enough data
            
            current_weight = self.get_current_signal_weight(signal_type)
            
            if perf["sharpe"] > 2.0 and perf["win_rate"] > 0.70:
                # This signal is crushing it — increase weight
                new_weight = min(current_weight * 1.10, current_weight + 0.05)
                adjustments.append(Adjustment(
                    param=f"signal_weight.{signal_type}",
                    old_value=current_weight,
                    new_value=new_weight,
                    reason=f"{signal_type}: {perf['win_rate']:.0%} win rate, "
                           f"{perf['sharpe']:.1f} Sharpe. Increasing weight."
                ))
            
            elif perf["sharpe"] < 0.8 or perf["win_rate"] < 0.50:
                # This signal is underperforming — decrease weight
                new_weight = max(current_weight * 0.85, 0.10)
                adjustments.append(Adjustment(
                    param=f"signal_weight.{signal_type}",
                    old_value=current_weight,
                    new_value=new_weight,
                    reason=f"{signal_type}: {perf['win_rate']:.0%} win rate, "
                           f"{perf['sharpe']:.1f} Sharpe. Reducing weight."
                ))
        
        # === 2. RETUNE SIGNAL THRESHOLDS ===
        # Did oversold_rsi at threshold 30 actually work? Maybe 28 is better.
        for signal_type, perf in signal_performance.items():
            if perf["trades"] >= 10:
                optimal_threshold = self.find_optimal_threshold(signal_type, trades)
                current_threshold = self.get_current_threshold(signal_type)
                
                if abs(optimal_threshold - current_threshold) / current_threshold > 0.05:
                    adjustments.append(Adjustment(
                        param=f"signal_threshold.{signal_type}",
                        old_value=current_threshold,
                        new_value=optimal_threshold,
                        reason=f"Backtested optimal threshold for {signal_type}: "
                               f"{optimal_threshold} (was {current_threshold})"
                    ))
        
        # === 3. ADJUST CONVICTION SIZING ===
        conviction_perf = self.compute_conviction_performance(trades)
        
        # If low-conviction trades are losing money, reduce their allocation
        if (conviction_perf["low"]["win_rate"] < 0.45 
            and conviction_perf["low"]["trades"] >= 10):
            adjustments.append(Adjustment(
                param="sizing.low_conviction_pct",
                old_value=self.params.sizing.low_conviction_pct,
                new_value=max(0.005, self.params.sizing.low_conviction_pct - 0.005),
                reason=f"Low conviction win rate {conviction_perf['low']['win_rate']:.0%}. "
                       f"Reducing allocation. Consider skipping entirely."
            ))
        
        # === 4. RETUNE SCOUT SOURCE CREDIBILITY ===
        scout_perf = self.compute_scout_source_performance(trades)
        
        for source, perf in scout_perf.items():
            if perf["trades"] >= 3:
                if perf["win_rate"] > 0.65:
                    adjustments.append(Adjustment(
                        param=f"scout.source_weight.{source}",
                        old_value=perf["current_weight"],
                        new_value=min(1.0, perf["current_weight"] + 0.05),
                        reason=f"Scout source {source}: {perf['win_rate']:.0%} win rate "
                               f"on {perf['trades']} trades. Upgrading credibility."
                    ))
                elif perf["win_rate"] < 0.40:
                    adjustments.append(Adjustment(
                        param=f"scout.source_weight.{source}",
                        old_value=perf["current_weight"],
                        new_value=max(0.10, perf["current_weight"] - 0.10),
                        reason=f"Scout source {source}: only {perf['win_rate']:.0%} win rate. "
                               f"Downgrading credibility."
                    ))
        
        # === 5. STRATEGY ALLOCATION SHIFT ===
        strategy_perf = self.compute_strategy_performance(trades)
        
        # If weeklies are outperforming monthlies, shift allocation
        for strat, perf in strategy_perf.items():
            if perf["sharpe"] > 2.0 and perf["allocation"] < 0.30:
                adjustments.append(Adjustment(
                    param=f"strategy_allocation.{strat}",
                    old_value=perf["allocation"],
                    new_value=min(0.30, perf["allocation"] + 0.03),
                    reason=f"{strat} Sharpe {perf['sharpe']:.1f} — earning more allocation."
                ))
        
        return adjustments
    
    def apply_adjustments(self, adjustments: list[Adjustment]):
        """
        Apply approved adjustments to trading_params.yaml.
        All adjustments are logged for audit trail.
        Cap each adjustment at max_adjustment_per_cycle.
        """
        for adj in adjustments:
            # Clamp adjustment magnitude
            max_change = abs(adj.old_value) * self.config.max_adjustment_per_cycle
            actual_change = adj.new_value - adj.old_value
            
            if abs(actual_change) > max_change:
                adj.new_value = adj.old_value + (max_change * (1 if actual_change > 0 else -1))
            
            self.update_param(adj.param, adj.new_value)
            self.log_adjustment(adj)
    
    def generate_learning_report(self, adjustments: list[Adjustment]) -> str:
        """
        Human-readable report of what changed and why.
        Included in the weekly review Telegram push.
        """
        report = "━━ WEEKLY LEARNING REPORT ━━\n\n"
        
        for adj in adjustments:
            direction = "↑" if adj.new_value > adj.old_value else "↓"
            report += (f"{direction} {adj.param}: {adj.old_value:.3f} → "
                      f"{adj.new_value:.3f}\n  {adj.reason}\n\n")
        
        return report
```

### Learning Loop SQL Schema

```sql
-- Track every parameter change for audit trail
CREATE TABLE parameter_adjustments (
    id SERIAL PRIMARY KEY,
    adjustment_date DATE NOT NULL,
    review_type VARCHAR(20) NOT NULL,     -- 'weekly' or 'monthly'
    param_name VARCHAR(100) NOT NULL,
    old_value DECIMAL(10,6),
    new_value DECIMAL(10,6),
    reason TEXT,
    approved BOOLEAN DEFAULT TRUE,        -- for human-in-the-loop review
    created_at TIMESTAMP DEFAULT NOW()
);

-- Track signal performance over time (for trend analysis)
CREATE TABLE signal_performance_history (
    id SERIAL PRIMARY KEY,
    week_ending DATE NOT NULL,
    signal_type VARCHAR(50) NOT NULL,
    trade_count INT,
    win_rate DECIMAL(5,4),
    avg_return DECIMAL(8,4),
    sharpe_ratio DECIMAL(6,3),
    optimal_threshold DECIMAL(10,4),
    created_at TIMESTAMP DEFAULT NOW()
);
```

---

## Live-Price Gate (replaces fixed timers)

Instead of "this alert expires in 30 minutes," every trade has CONDITIONS 
that must be true at the moment you tap EXECUTE. Miss it by 3 minutes? Fine.
Miss it by 3 hours? Also fine — IF the conditions still hold.

```python
@dataclass
class LivePriceGate:
    """
    Every trade opportunity has a set of conditions that are checked
    in real-time when the user taps EXECUTE. No fixed timer.
    
    The trade is valid for as long as conditions hold — could be 
    5 minutes or 5 hours. If the stock bounces and premiums thin, 
    the gate closes automatically regardless of time elapsed.
    """
    symbol: str
    trade_type: str
    strike: float
    expiration: date
    
    # Original analysis context
    analysis_time: datetime
    analysis_price: float
    analysis_premium: float
    signals: list[AlphaSignal]
    conviction: str
    
    # === GATE CONDITIONS (ALL must pass at execution time) ===
    
    # 1. Underlying must be within this range
    #    Set to ±3% of analysis price by default
    #    Wider range for HIGH conviction, tighter for LOW
    underlying_floor: float       # stock can't have bounced too much (premium thin)
    underlying_ceiling: float     # stock can't have crashed further (new thesis needed)
    
    # 2. Premium must still be rich enough
    #    Set to 80% of analysis premium by default
    min_premium: float
    
    # 3. IV rank must still be elevated
    #    If IV crushed back down, the setup is gone
    min_iv_rank: float
    
    # 4. Delta must be within acceptable range
    #    If delta shifted past max, risk/reward changed
    max_abs_delta: float
    
    # 5. No new disqualifying events since analysis
    #    (earnings announced, halt, etc.)
    disqualifying_events: list[str] = None
    
    # Hard safety limits (overrides everything)
    max_age_hours: float = 8.0    # absolute max: same trading day
    market_must_be_open: bool = True


def create_gate_from_opportunity(
    opp: SizedOpportunity,
    mkt: MarketContext
) -> LivePriceGate:
    """
    When the analysis engine produces a trade, automatically 
    create a gate with appropriate conditions.
    """
    
    # Range depends on conviction — high conviction = wider tolerance
    range_pct = {
        "high": 0.04,    # ±4% — you really want this trade, give it room
        "medium": 0.03,  # ±3% — standard
        "low": 0.02      # ±2% — marginal setup, tight conditions
    }[opp.conviction]
    
    return LivePriceGate(
        symbol=opp.symbol,
        trade_type=opp.trade_type,
        strike=opp.strike,
        expiration=opp.expiration,
        analysis_time=datetime.now(),
        analysis_price=mkt.price,
        analysis_premium=opp.premium,
        signals=opp.signals,
        conviction=opp.conviction,
        underlying_floor=mkt.price * (1 - range_pct),
        underlying_ceiling=mkt.price * (1 + range_pct),
        min_premium=opp.premium * 0.80,  # 80% of original premium
        min_iv_rank=max(mkt.iv_rank - 10, 30),  # IV rank can drop 10 pts
        max_abs_delta=0.45,  # don't execute if delta shifted past 0.45
    )


async def validate_gate_at_execution(
    gate: LivePriceGate
) -> tuple[bool, str, dict]:
    """
    Called the INSTANT the user taps EXECUTE.
    Takes ~2 seconds to validate against live market.
    Returns (valid, reason, live_data).
    """
    
    # Check age (same-day safety)
    age_hours = (datetime.now() - gate.analysis_time).total_seconds() / 3600
    if age_hours > gate.max_age_hours:
        return (False, "Alert expired (end of trading day). Will re-evaluate tomorrow.", {})
    
    if gate.market_must_be_open and not is_market_open():
        return (False, "Market is closed. Order will queue for tomorrow's open.", {})
    
    # Pull live data (2-second operation)
    live_quote = await get_live_quote(gate.symbol)
    live_chain = await get_live_option(gate.symbol, gate.strike, gate.expiration)
    live_iv_rank = await get_live_iv_rank(gate.symbol)
    
    live_data = {
        "price": live_quote.price,
        "premium": live_chain.mid_price,
        "delta": live_chain.delta,
        "iv_rank": live_iv_rank,
        "bid": live_chain.bid,
        "ask": live_chain.ask,
        "spread": live_chain.ask - live_chain.bid,
    }
    
    # === CHECK EACH GATE CONDITION ===
    
    # 1. Underlying range
    if live_data["price"] < gate.underlying_floor:
        return (False, 
                f"Stock dropped further to ${live_data['price']:.2f} "
                f"(below ${gate.underlying_floor:.2f} floor). "
                f"New analysis needed — thesis may have changed.", 
                live_data)
    
    if live_data["price"] > gate.underlying_ceiling:
        return (False,
                f"Stock bounced to ${live_data['price']:.2f} "
                f"(above ${gate.underlying_ceiling:.2f} ceiling). "
                f"The dip you were selling into has recovered. Premium is thin.",
                live_data)
    
    # 2. Premium floor
    if live_data["premium"] < gate.min_premium:
        return (False,
                f"Premium dropped to ${live_data['premium']:.2f} "
                f"(minimum ${gate.min_premium:.2f}). Not worth the risk/reward.",
                live_data)
    
    # 3. IV rank
    if live_data["iv_rank"] < gate.min_iv_rank:
        return (False,
                f"IV rank fell to {live_data['iv_rank']:.0f} "
                f"(minimum {gate.min_iv_rank:.0f}). Premiums no longer rich.",
                live_data)
    
    # 4. Delta
    if abs(live_data["delta"]) > gate.max_abs_delta:
        return (False,
                f"Delta shifted to {live_data['delta']:.2f}. "
                f"Too close to the money — risk/reward no longer favorable.",
                live_data)
    
    # 5. Spread check (not in original gate but always worth checking)
    spread_pct = live_data["spread"] / live_data["premium"] if live_data["premium"] else 1
    if spread_pct > 0.10:
        return (False,
                f"Bid-ask spread is {spread_pct:.0%} of premium. "
                f"Too wide — you'll get a bad fill. Wait for tighter spread.",
                live_data)
    
    # ALL GATES PASSED
    return (True, "All conditions met. Executing at live price.", live_data)
```

### Telegram Alert Format (Live-Price Gate)

```python
def format_gated_alert(gate: LivePriceGate, mkt: MarketContext) -> str:
    """
    Alert format emphasizes CONDITIONS not TIME.
    The user sees what must be true, not when it expires.
    """
    return f"""
⚡ {gate.symbol} | {gate.conviction.upper()} conviction

SELL {gate.contracts}x {gate.symbol} {gate.strike}P {gate.expiration}
Premium now: ${gate.analysis_premium:.2f}

VALID WHILE:
  {gate.symbol} ${gate.underlying_floor:.2f} – ${gate.underlying_ceiling:.2f}  (now ${mkt.price:.2f} ✅)
  Premium ≥ ${gate.min_premium:.2f}    (now ${gate.analysis_premium:.2f} ✅)
  IV rank ≥ {gate.min_iv_rank:.0f}       (now {mkt.iv_rank:.0f} ✅)

SIGNALS: {', '.join(s.signal_type.value for s in gate.signals[:3])}

Tap EXECUTE anytime — system validates live price first.
No rush. Conditions protect you, not a clock.

[EXECUTE]  [SKIP]
"""


def format_execution_result(
    gate: LivePriceGate, 
    valid: bool, 
    reason: str, 
    live_data: dict
) -> str:
    """Message sent after user taps EXECUTE."""
    
    if valid:
        return f"""
✅ EXECUTED: {gate.symbol} {gate.strike}P

Limit order placed at ${live_data['premium'] - 0.01:.2f}
Underlying: ${live_data['price']:.2f}
IV rank: {live_data['iv_rank']:.0f}

All gate conditions passed. Order is live on E*Trade.
"""
    else:
        return f"""
❌ BLOCKED: {gate.symbol} {gate.strike}P

{reason}

Current: ${live_data.get('price', 0):.2f} | Premium: ${live_data.get('premium', 0):.2f}

The system protected you from a stale trade.
It will re-evaluate in the next analysis cycle.
"""
```

### Auto-Execution Graduation

```python
@dataclass
class AutoExecutionPolicy:
    """
    Phased approach to automation. Start manual, graduate to auto 
    as the system proves itself over your paper trading period.
    """
    
    # Phase 1: Paper Trading (Weeks 1-8)
    # Everything is simulated. No real orders.
    paper_trading: bool = True
    
    # Phase 2: Manual Approval (Months 1-2 of live trading)
    # Every trade requires a tap on Telegram.
    manual_approval: bool = True
    
    # Phase 3: Selective Auto (Month 3+)
    # Certain trade types auto-execute. Others stay manual.
    auto_close_winners: bool = False      # close at 50%+ profit
    auto_close_loss_stops: bool = False   # close at 2x loss stop
    auto_execute_high_conviction: bool = False
    auto_execute_medium_conviction: bool = False
    auto_execute_scout_picks: bool = False  # always manual
    
    # Safety: max auto-executed trades per day
    max_auto_trades_per_day: int = 3
    max_auto_capital_per_day: float = 0.10  # max 10% of NLV auto-deployed/day


# Recommended graduation timeline
GRADUATION_TIMELINE = """
Weeks 1-8:     PAPER TRADING
               Everything simulated. Build confidence in signals.
               Target: 60+ paper trades, validate win rate and sizing.

Months 1-2:    FULLY MANUAL (live money)
               Tap EXECUTE on every trade. Get comfortable with the flow.
               Compare your decisions to system recommendations.

Month 3:       AUTO-CLOSE WINNERS + LOSS STOPS
               50%+ profit closes and 2x loss stops auto-execute.
               These are mechanical, no-brainer decisions.
               Notification becomes informational, not approval.

Month 4:       AUTO-EXECUTE HIGH CONVICTION
               Trades with 2+ confirming signals and HIGH conviction
               auto-execute (with live-price gate validation).
               Still tap-to-approve for MEDIUM and LOW.

Month 5+:      AUTO HIGH + MEDIUM
               Only LOW conviction and Scout picks need manual approval.
               CRITICAL alerts (loss stops, expiry risk) always auto.

NEVER AUTO:    Scout Agent picks, first trade in a new ticker,
               trades during CRISIS regime, positions >3% of NLV.
"""
```

---

## Paper Trading Framework

Paper trading is not optional. You MUST validate the system against live market 
conditions before risking real money. The paper trader simulates everything except 
the actual E*Trade order — same signals, same sizing, same gates, same alerts.

### How Long to Paper Trade

```
MINIMUM: 4 weeks (1 full monthly expiration cycle)
RECOMMENDED: 8 weeks (2 full cycles)
IDEAL: 8 weeks + at least one of these stress events:
  - A 3%+ single-day market drop
  - An earnings miss on a name you have positions on
  - A VIX spike above 25
  - A losing streak of 3+ trades

You need AT LEAST 60 paper trades before going live.
At 3-5 trades/day, that's 12-20 trading days — about 3-4 weeks.
8 weeks gives you ~100-150 trades which is enough for statistical
confidence on win rates and strategy performance.

GO-LIVE CHECKLIST (all must be true):
  □ 60+ paper trades completed
  □ Overall win rate ≥ 55%
  □ Sharpe ratio ≥ 1.2 (risk-adjusted)
  □ Max drawdown during paper period < 12%
  □ High-conviction win rate ≥ 65%
  □ At least one losing week experienced and survived
  □ System correctly identified and handled a VIX spike
  □ Loss management rules triggered at least 3 times
  □ You trust the system enough to tap EXECUTE without hesitation
```

### Paper Trader Architecture

```python
class PaperTrader:
    """
    Simulates the full trading system without placing real orders.
    Uses live market data so the simulation reflects real conditions.
    
    The paper trader:
    - Receives the same alerts you'd get in production
    - Records simulated entries at the live market price
    - Tracks simulated P&L using real price movements
    - Applies the same profit/loss rules as production
    - Generates the same briefings and reviews
    
    The ONLY difference: no E*Trade order is placed.
    Everything else is identical to live trading.
    """
    
    def __init__(self, initial_capital: float = 100_000):
        self.initial_capital = initial_capital
        self.current_capital = initial_capital
        self.buying_power = initial_capital
        
        # Simulated positions
        self.open_positions: list[PaperPosition] = []
        self.closed_positions: list[PaperPosition] = []
        
        # Performance tracking
        self.daily_snapshots: list[PaperSnapshot] = []
        self.trade_log: list[PaperTrade] = []
        
        # Simulation mode
        self.mode: str = "shadow"  # "shadow" or "standalone"
        # shadow: runs alongside real portfolio, compares decisions
        # standalone: runs against simulated capital only
    
    async def on_alert(self, gate: LivePriceGate, sized: SizedOpportunity):
        """
        Called when the system generates a trade alert.
        In paper mode, simulates the execution instead of sending to E*Trade.
        """
        # Simulate execution at current market price
        live_data = await get_live_market_data(gate.symbol, gate.strike, gate.expiration)
        
        # Validate gate (same logic as production)
        valid, reason, data = await validate_gate_at_execution(gate)
        
        if not valid:
            self.log_event("PAPER_SKIP", gate.symbol, reason)
            return
        
        # Simulate fill at mid price + 1 cent slippage
        fill_price = data["premium"] - 0.01  # realistic slippage
        
        position = PaperPosition(
            symbol=gate.symbol,
            trade_type=gate.trade_type,
            strike=gate.strike,
            expiration=gate.expiration,
            entry_price=fill_price,
            entry_time=datetime.now(),
            contracts=sized.contracts,
            conviction=sized.conviction,
            signals=[s.signal_type.value for s in gate.signals],
            strategy=sized.strategy_type,
            capital_at_risk=sized.capital_deployed,
            max_profit=fill_price * sized.contracts * 100,
        )
        
        self.open_positions.append(position)
        self.buying_power -= sized.capital_deployed
        
        self.log_trade("PAPER_OPEN", position)
        
        # Send notification (marked as PAPER)
        await send_telegram(f"""
📝 PAPER TRADE OPENED: {gate.symbol}

SELL {sized.contracts}x {gate.strike}P @ ${fill_price:.2f}
Capital deployed: ${sized.capital_deployed:,.0f}
Conviction: {sized.conviction.upper()}

[This is a simulated trade — no real order placed]
""")
    
    async def update_positions(self):
        """
        Run every minute during market hours.
        Update P&L on all paper positions using live prices.
        Check profit targets and loss stops.
        """
        for pos in self.open_positions[:]:  # copy to allow removal during iteration
            live_option = await get_live_option_price(
                pos.symbol, pos.strike, pos.expiration, pos.trade_type
            )
            
            pos.current_price = live_option.mid_price
            pos.current_pnl = (pos.entry_price - pos.current_price) * pos.contracts * 100
            pos.profit_pct = pos.current_pnl / pos.max_profit if pos.max_profit else 0
            
            # Check profit target
            if pos.profit_pct >= 0.50 and pos.days_to_expiry > 14:
                await self.close_paper_position(pos, "profit_target_50pct")
            
            # Check loss stop
            loss_multiple = pos.current_price / pos.entry_price
            max_mult = 1.5 if pos.days_to_expiry <= 10 else 2.0
            if loss_multiple >= max_mult:
                await self.close_paper_position(pos, f"loss_stop_{max_mult}x")
            
            # Check expiration
            if pos.days_to_expiry <= 0:
                if pos.in_the_money:
                    await self.close_paper_position(pos, "assigned")
                else:
                    await self.close_paper_position(pos, "expired_worthless")
    
    async def close_paper_position(self, pos: PaperPosition, reason: str):
        """Simulate closing a position."""
        pos.exit_price = pos.current_price
        pos.exit_time = datetime.now()
        pos.exit_reason = reason
        pos.final_pnl = (pos.entry_price - pos.exit_price) * pos.contracts * 100
        
        self.open_positions.remove(pos)
        self.closed_positions.append(pos)
        self.buying_power += pos.capital_at_risk
        self.current_capital += pos.final_pnl
        
        self.log_trade("PAPER_CLOSE", pos)
        
        emoji = "💰" if pos.final_pnl > 0 else "📉"
        await send_telegram(f"""
{emoji} PAPER TRADE CLOSED: {pos.symbol} {pos.strike}P

{reason.replace('_', ' ').title()}
Entry: ${pos.entry_price:.2f} → Exit: ${pos.exit_price:.2f}
P&L: ${pos.final_pnl:+,.0f} ({pos.profit_pct:+.0%})
Conviction was: {pos.conviction.upper()}

[Paper trade — no real money involved]
""")


@dataclass
class PaperPosition:
    symbol: str
    trade_type: str
    strike: float
    expiration: date
    entry_price: float
    entry_time: datetime
    contracts: int
    conviction: str
    signals: list[str]
    strategy: str
    capital_at_risk: float
    max_profit: float
    
    # Updated in real-time
    current_price: float = 0.0
    current_pnl: float = 0.0
    profit_pct: float = 0.0
    
    # Set at close
    exit_price: float | None = None
    exit_time: datetime | None = None
    exit_reason: str | None = None
    final_pnl: float | None = None
    
    @property
    def days_to_expiry(self) -> int:
        return (self.expiration - date.today()).days
    
    @property
    def in_the_money(self) -> bool:
        # For short puts: ITM when underlying < strike
        return self.current_underlying < self.strike
```

### Paper Trading Dashboard

```python
def generate_paper_dashboard(trader: PaperTrader) -> str:
    """
    Weekly paper trading review. This is what tells you 
    whether the system is ready for real money.
    """
    closed = trader.closed_positions
    if not closed:
        return "No paper trades closed yet. Keep running."
    
    winners = [p for p in closed if p.final_pnl > 0]
    losers = [p for p in closed if p.final_pnl <= 0]
    
    win_rate = len(winners) / len(closed)
    total_pnl = sum(p.final_pnl for p in closed)
    avg_winner = sum(p.final_pnl for p in winners) / len(winners) if winners else 0
    avg_loser = sum(p.final_pnl for p in losers) / len(losers) if losers else 0
    profit_factor = abs(sum(p.final_pnl for p in winners) / 
                        sum(p.final_pnl for p in losers)) if losers else float('inf')
    
    # Performance by conviction
    high_trades = [p for p in closed if p.conviction == "high"]
    med_trades = [p for p in closed if p.conviction == "medium"]
    low_trades = [p for p in closed if p.conviction == "low"]
    
    # Drawdown calculation
    equity_curve = []
    running_pnl = 0
    for p in sorted(closed, key=lambda x: x.exit_time):
        running_pnl += p.final_pnl
        equity_curve.append(running_pnl)
    
    peak = 0
    max_dd = 0
    for val in equity_curve:
        peak = max(peak, val)
        dd = (peak - val) / trader.initial_capital
        max_dd = max(max_dd, dd)
    
    # Annualized return estimate
    days_elapsed = (date.today() - closed[0].entry_time.date()).days or 1
    annualized = (total_pnl / trader.initial_capital) * (365 / days_elapsed)
    
    # Go-live checklist
    checks = {
        "60+ trades": len(closed) >= 60,
        "Win rate ≥ 55%": win_rate >= 0.55,
        "Sharpe ≥ 1.2": True,  # computed separately
        "Max DD < 12%": max_dd < 0.12,
        "High conviction WR ≥ 65%": (
            len([p for p in high_trades if p.final_pnl > 0]) / len(high_trades) >= 0.65
            if high_trades else False
        ),
        "Loss mgmt triggered 3+": (
            len([p for p in closed if p.exit_reason and "loss_stop" in p.exit_reason]) >= 3
        ),
    }
    
    return f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PAPER TRADING DASHBOARD — Week {days_elapsed // 7 + 1}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PERFORMANCE SUMMARY:
  Total trades: {len(closed)}
  Win rate: {win_rate:.0%}
  Total P&L: ${total_pnl:+,.0f}
  Annualized pace: {annualized:+.0%}
  
  Avg winner: ${avg_winner:+,.0f}
  Avg loser: ${avg_loser:+,.0f}
  Profit factor: {profit_factor:.2f}
  Max drawdown: {max_dd:.1%}

BY CONVICTION:
  HIGH:   {len(high_trades)} trades, {len([p for p in high_trades if p.final_pnl > 0])}/{len(high_trades)} won ({len([p for p in high_trades if p.final_pnl > 0])/len(high_trades)*100:.0f}% WR) if high_trades else 'N/A'
  MEDIUM: {len(med_trades)} trades, {len([p for p in med_trades if p.final_pnl > 0])}/{len(med_trades)} won
  LOW:    {len(low_trades)} trades, {len([p for p in low_trades if p.final_pnl > 0])}/{len(low_trades)} won

GO-LIVE CHECKLIST:
{chr(10).join(f'  {"✅" if passed else "❌"} {check}' for check, passed in checks.items())}

{"🟢 READY FOR LIVE TRADING" if all(checks.values()) else f"🟡 {sum(checks.values())}/{len(checks)} checks passed. Keep paper trading."}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
```

### Paper Trading Config

```yaml
paper_trading:
  enabled: true
  initial_capital: 100000          # simulated starting capital
  mode: "shadow"                    # "shadow" (alongside real portfolio) or "standalone"
  
  # Simulation realism
  slippage_per_contract: 0.03      # 3 cents per contract
  commission_per_contract: 0.65     # E*Trade standard
  fill_probability: 0.95            # 95% chance limit order fills at mid
  
  # Alerts (same format as production, marked as PAPER)
  send_paper_alerts: true
  send_paper_dashboard_weekly: true
  dashboard_day: "sunday"
  
  # Go-live criteria
  min_trades_for_golive: 60
  min_win_rate: 0.55
  max_drawdown_allowed: 0.12
  min_high_conviction_wr: 0.65
  min_loss_stops_triggered: 3
  
  # Duration
  min_duration_weeks: 4
  recommended_duration_weeks: 8
```

### Paper Trading SQL Schema

```sql
CREATE TABLE paper_trades (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(10) NOT NULL,
    trade_type VARCHAR(20) NOT NULL,
    strike DECIMAL(10,2),
    expiration DATE,
    contracts INT,
    conviction VARCHAR(10),
    strategy VARCHAR(30),
    signals TEXT[],                      -- array of signal types that triggered
    
    -- Entry
    entry_price DECIMAL(10,4),
    entry_time TIMESTAMP,
    entry_underlying DECIMAL(10,2),
    entry_iv_rank DECIMAL(5,1),
    capital_at_risk DECIMAL(12,2),
    
    -- Exit
    exit_price DECIMAL(10,4),
    exit_time TIMESTAMP,
    exit_underlying DECIMAL(10,2),
    exit_reason VARCHAR(50),
    
    -- P&L
    pnl DECIMAL(12,2),
    pnl_pct DECIMAL(8,4),
    
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE paper_daily_snapshots (
    id SERIAL PRIMARY KEY,
    date DATE NOT NULL,
    capital DECIMAL(12,2),
    buying_power DECIMAL(12,2),
    open_positions INT,
    daily_pnl DECIMAL(12,2),
    cumulative_pnl DECIMAL(12,2),
    max_drawdown DECIMAL(8,4),
    win_rate DECIMAL(5,4),
    trades_to_date INT,
    created_at TIMESTAMP DEFAULT NOW()
);
```

---

## Drawdown Decomposition

When the portfolio drops, you need to know WHY — one blowup vs. correlation
drag vs. regime misjudgment. The answer determines your response.

```python
@dataclass
class DrawdownDecomposition:
    """
    Break down any portfolio drawdown into its component causes.
    This is how you learn from losses instead of just suffering them.
    """
    
    period_start: date
    period_end: date
    total_drawdown_pct: float              # total portfolio drawdown
    total_drawdown_dollars: float
    
    # === DECOMPOSITION BY CAUSE ===
    
    # 1. Individual position blowups
    single_position_losses: list[dict]     # [{symbol, loss_pct, loss_dollars, cause}]
    largest_single_loss: dict              # the one that hurt most
    single_position_contribution: float    # what % of drawdown came from blowups
    
    # 2. Correlation drag (market moved against you)
    market_move_pct: float                 # how much SPY moved in this period
    beta_explained_loss: float             # loss explained by portfolio beta × market move
    correlation_contribution: float        # what % of drawdown was just beta exposure
    
    # 3. Volatility regime error
    iv_move_pct: float                     # how much VIX moved
    vega_explained_loss: float             # loss from being short vega during IV spike
    vega_contribution: float               # what % was vega
    
    # 4. Strategy-specific breakdown
    strategy_losses: dict[str, float]      # {strategy: loss_dollars}
    worst_strategy: str
    
    # 5. Signal quality
    losses_from_high_conviction: float     # did strong signals fail?
    losses_from_low_conviction: float      # did marginal trades drag you?
    signal_types_that_failed: list[str]    # which signals produced losers
    
    # === DIAGNOSIS ===
    primary_cause: str                     # "single_blowup", "correlation", "vega", 
                                           # "bad_signals", "overconcentration"
    recommended_action: str                # specific fix


def decompose_drawdown(
    trades: list[TradeRecord],
    snapshots: list[DailySnapshot],
    market_data: dict
) -> DrawdownDecomposition:
    """
    Run after any drawdown exceeding 5%.
    Produces a diagnosis + recommended action.
    """
    
    # Find the peak and trough
    peak_snapshot = max(snapshots, key=lambda s: s.net_liquidation)
    trough_snapshot = min(
        [s for s in snapshots if s.date > peak_snapshot.date],
        key=lambda s: s.net_liquidation
    )
    
    total_loss = peak_snapshot.net_liquidation - trough_snapshot.net_liquidation
    total_pct = total_loss / peak_snapshot.net_liquidation
    
    # Isolate trades during the drawdown period
    dd_trades = [t for t in trades 
                 if peak_snapshot.date <= t.close_date <= trough_snapshot.date
                 and t.pnl < 0]
    
    # Single position analysis
    single_losses = []
    for t in dd_trades:
        single_losses.append({
            "symbol": t.symbol,
            "loss_pct": t.pnl / peak_snapshot.net_liquidation,
            "loss_dollars": t.pnl,
            "cause": t.loss_reason,  # "tested_strike", "earnings_gap", "crash", etc.
            "signal_type": t.entry_signal_type,
            "conviction": t.entry_conviction
        })
    
    single_contribution = sum(s["loss_dollars"] for s in single_losses) / total_loss
    
    # Beta explanation
    spy_move = (market_data["spy_trough"] - market_data["spy_peak"]) / market_data["spy_peak"]
    portfolio_beta = sum(s.portfolio_beta_delta for s in snapshots) / len(snapshots)
    beta_explained = spy_move * portfolio_beta * peak_snapshot.net_liquidation
    correlation_contribution = abs(beta_explained) / total_loss
    
    # Vega explanation  
    vix_move = market_data["vix_trough_period"] - market_data["vix_peak_period"]
    avg_vega = sum(s.portfolio_vega for s in snapshots) / len(snapshots)
    vega_explained = vix_move * avg_vega
    vega_contribution = abs(vega_explained) / total_loss if total_loss else 0
    
    # Diagnose primary cause
    contributions = {
        "single_blowup": 0,
        "correlation": correlation_contribution,
        "vega": vega_contribution,
        "bad_signals": 0,
        "overconcentration": 0
    }
    
    # Check if one position was responsible for >40% of loss
    if single_losses:
        worst = max(single_losses, key=lambda s: abs(s["loss_dollars"]))
        if abs(worst["loss_dollars"]) / total_loss > 0.40:
            contributions["single_blowup"] = abs(worst["loss_dollars"]) / total_loss
    
    # Check if high-conviction signals failed
    high_conv_losses = sum(abs(s["loss_dollars"]) for s in single_losses 
                          if s["conviction"] == "high")
    if high_conv_losses / total_loss > 0.30:
        contributions["bad_signals"] = high_conv_losses / total_loss
    
    primary_cause = max(contributions, key=contributions.get)
    
    # Generate recommendation based on cause
    recommendations = {
        "single_blowup": (
            f"One position ({worst['symbol']}) caused {contributions['single_blowup']:.0%} "
            f"of the drawdown. Review the entry signal — was it valid? "
            f"Tighten max loss rules for this type of trade. "
            f"Consider reducing max position size from 5% to 3%."
        ),
        "correlation": (
            f"Market correlation explained {correlation_contribution:.0%} of the drawdown. "
            f"Your portfolio beta was {portfolio_beta:.2f} when the market dropped "
            f"{abs(spy_move):.1%}. Reduce beta exposure: add hedges, reduce position "
            f"count in correlated clusters, or shift more to Engine 3."
        ),
        "vega": (
            f"IV expansion caused {vega_contribution:.0%} of the drawdown. "
            f"You were too short vega when VIX spiked from {market_data['vix_peak_period']:.0f} "
            f"to {market_data['vix_trough_period']:.0f}. Buy protective VIX calls "
            f"or reduce short option count in high-VIX environments."
        ),
        "bad_signals": (
            f"High-conviction signals failed, causing {contributions['bad_signals']:.0%} "
            f"of losses. Failed signals: {', '.join(set(s['signal_type'] for s in single_losses if s['conviction'] == 'high'))}. "
            f"Run backtests on these signals. Consider reducing their weight "
            f"or tightening their thresholds."
        ),
        "overconcentration": (
            "Multiple positions in the same sector/correlation cluster all lost simultaneously. "
            "Tighten correlation caps and reduce effective concentration."
        )
    }
    
    return DrawdownDecomposition(
        period_start=peak_snapshot.date,
        period_end=trough_snapshot.date,
        total_drawdown_pct=total_pct,
        total_drawdown_dollars=total_loss,
        single_position_losses=single_losses,
        largest_single_loss=worst if single_losses else {},
        single_position_contribution=single_contribution,
        market_move_pct=spy_move,
        beta_explained_loss=beta_explained,
        correlation_contribution=correlation_contribution,
        iv_move_pct=vix_move,
        vega_explained_loss=vega_explained,
        vega_contribution=vega_contribution,
        strategy_losses={},  # computed above
        worst_strategy="",
        losses_from_high_conviction=high_conv_losses,
        losses_from_low_conviction=total_loss - high_conv_losses,
        signal_types_that_failed=[s["signal_type"] for s in single_losses if s["conviction"] == "high"],
        primary_cause=primary_cause,
        recommended_action=recommendations[primary_cause]
    )
```

### Drawdown Report Format

```
━━ DRAWDOWN ANALYSIS: April 2-11, 2026 ━━

Total drawdown: -8.2% (-$61,500)

DECOMPOSITION:
  Market correlation (SPY -5.1%):  52% of loss ($32K)
  IV spike (VIX 19→28):           23% of loss ($14K)
  NVDA single blowup:             18% of loss ($11K)
  Other position losses:            7% of loss ($4.5K)

PRIMARY CAUSE: Correlation exposure
  Portfolio beta was 1.42 during a -5.1% SPY move.
  25 names, but only 7 effective positions after correlation.

DIAGNOSIS:
  ✅ Signal quality was fine — entries were valid
  ⚠️ Position sizing was fine — no single outsized bet
  ❌ Correlation was the problem — too much tech/semi overlap
  ❌ No tail hedge in place — would have saved ~$8K

RECOMMENDED ACTIONS:
  1. Buy SPY May 5% OTM put ($1,200) as tail hedge
  2. Close 1 of {NVDA, AMD, AVGO, TSM} to reduce semi cluster
  3. Add 2 non-tech positions to Engine 1 for diversification
  4. Target portfolio beta ≤1.20 in current regime
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## Continuous Market Monitor

The morning briefing is a snapshot. But the best entries happen intraday — a stock 
gaps down at 10:30am, a Fed speaker spooks the market at 2pm, a sector rotates hard 
in the final hour. A daily-only system misses all of these.

### Three Operating Modes

```
 PRE-MARKET          MARKET HOURS               POST-MARKET
 ──────────   ──────────────────────────────   ─────────────
 8:00 AM      9:30 ──────────────────── 4:00    4:30 PM
 │            │                              │   │
 Morning      Continuous Monitor              │   Post-Market
 Briefing     (always-on, real-time)          │   Review
 │            │                              │   │
 Plan of      Lightweight tripwires detect    │   What happened?
 the day      events → full analysis on       │   What's valid
              triggered tickers only          │   for tomorrow?
```

### Continuous Monitor Architecture

```python
class ContinuousMonitor:
    """
    Always-on process during market hours (9:30 AM - 4:00 PM ET).
    
    NOT a full analysis every second. Instead:
    - Lightweight price/volume polling every 30 seconds
    - Tripwires that detect "something interesting just happened"
    - When a tripwire fires → spin up full analysis on that ticker only
    - Full analysis → alert if it qualifies
    
    This is the difference between watching 50 screens and having 
    50 alarms that only go off when something matters.
    """
    
    # Polling intervals by data type
    PRICE_POLL_INTERVAL = 30        # seconds — stock prices
    IV_POLL_INTERVAL = 300          # 5 minutes — IV rank recalculation
    NEWS_POLL_INTERVAL = 120        # 2 minutes — news/social feeds
    PORTFOLIO_POLL_INTERVAL = 60    # 1 minute — position P&L updates
    
    def __init__(self):
        self.watchlist: list[str] = load_watchlist()
        self.portfolio: PortfolioState = None
        self.tripwire_states: dict[str, TripwireState] = {}
        self.alerts_sent_today: list[str] = []
        self.max_alerts_per_day: int = 8   # don't spam yourself
    
    async def run(self):
        """Main event loop during market hours."""
        while is_market_open():
            # Refresh portfolio state
            if time_since_last(self.PORTFOLIO_POLL_INTERVAL):
                self.portfolio = await refresh_portfolio()
                await self.check_position_tripwires()
            
            # Check price tripwires (fastest loop)
            if time_since_last(self.PRICE_POLL_INTERVAL):
                await self.check_price_tripwires()
            
            # Check IV surface changes (slower loop)
            if time_since_last(self.IV_POLL_INTERVAL):
                await self.check_iv_tripwires()
            
            # Check news/social feeds
            if time_since_last(self.NEWS_POLL_INTERVAL):
                await self.check_news_tripwires()
            
            await asyncio.sleep(5)  # main loop tick


@dataclass
class TripwireState:
    """Track the state of each tripwire per symbol."""
    symbol: str
    last_price: float
    last_iv_rank: float
    last_rsi: float
    last_check_time: datetime
    
    # Thresholds (lightweight — not full signal detection)
    price_move_threshold: float = 0.025    # 2.5% move triggers full analysis
    iv_rank_change_threshold: float = 15    # 15-point IV rank change
    rsi_threshold: float = 30               # RSI crossing below 30
    volume_spike_threshold: float = 3.0     # 3x average volume
```

### Price & Volume Tripwires (30-second loop)

```python
class PriceTripwires:
    """
    Lightweight checks that run every 30 seconds.
    These are NOT full signal detection — they're alarms that 
    something worth analyzing just happened.
    """
    
    async def check(self, monitor: ContinuousMonitor) -> list[str]:
        """Returns list of symbols that tripped a wire."""
        tripped = []
        
        # Batch quote — E*Trade allows 25 symbols per call
        quotes = await get_batch_quotes(monitor.watchlist)
        
        for symbol, quote in quotes.items():
            state = monitor.tripwire_states.get(symbol)
            if not state:
                monitor.tripwire_states[symbol] = TripwireState(
                    symbol=symbol, 
                    last_price=quote.price,
                    last_iv_rank=0, last_rsi=50,
                    last_check_time=datetime.now()
                )
                continue
            
            # === INTRADAY DIP TRIPWIRE ===
            # Stock dropped 2.5%+ since market open
            open_price = quote.open_price
            current_drop = (quote.price - open_price) / open_price
            if current_drop <= -state.price_move_threshold:
                tripped.append(symbol)
                continue
            
            # === SUDDEN DROP TRIPWIRE ===
            # Stock dropped 1.5%+ in the last 15 minutes
            # (using last_price from 30 seconds ago, accumulates)
            recent_drop = (quote.price - state.last_price) / state.last_price
            if recent_drop <= -0.015:
                tripped.append(symbol)
                continue
            
            # === VOLUME SPIKE TRIPWIRE ===
            # Current volume already exceeds 3x daily average
            # (heavy selling or buying — something is happening)
            if (quote.volume and quote.avg_volume and 
                quote.volume > quote.avg_volume * state.volume_spike_threshold):
                tripped.append(symbol)
                continue
            
            # === BOUNCE FROM LOW TRIPWIRE ===
            # Stock was down 5%+ and just bounced 1%+ off intraday low
            # This is the "dip is done" signal — ideal entry timing
            if quote.day_low and open_price:
                max_drop = (quote.day_low - open_price) / open_price
                bounce_from_low = (quote.price - quote.day_low) / quote.day_low
                if max_drop <= -0.05 and bounce_from_low >= 0.01:
                    tripped.append(symbol)
                    continue
            
            # Update state
            state.last_price = quote.price
            state.last_check_time = datetime.now()
        
        return tripped
```

### IV Surface Tripwires (5-minute loop)

```python
class IVTripwires:
    """
    IV changes more slowly than price but is critical for premium selling.
    Check every 5 minutes.
    """
    
    async def check(self, monitor: ContinuousMonitor) -> list[str]:
        tripped = []
        
        for symbol in monitor.watchlist:
            state = monitor.tripwire_states.get(symbol)
            if not state:
                continue
            
            current_iv = await get_current_iv(symbol)
            current_iv_rank = calculate_iv_rank_quick(symbol, current_iv)
            
            # === IV RANK SPIKE INTRADAY ===
            # IV rank jumped 15+ points since morning
            if current_iv_rank - state.last_iv_rank >= state.iv_rank_change_threshold:
                tripped.append(symbol)
            
            # === IV RANK CROSSED KEY THRESHOLD ===
            # Crossed above 50 (was below, now above — premiums getting rich)
            if state.last_iv_rank < 50 and current_iv_rank >= 50:
                tripped.append(symbol)
            
            # === IV RANK CROSSED EXTREME ===
            # Crossed above 70 (fear territory — maximum premium harvest)
            if state.last_iv_rank < 70 and current_iv_rank >= 70:
                tripped.append(symbol)
            
            state.last_iv_rank = current_iv_rank
        
        return tripped
```

### Position Tripwires (1-minute loop)

```python
class PositionTripwires:
    """
    Monitor YOUR open positions for situations that need immediate attention.
    This catches things the morning briefing can't — positions that go 
    sideways intraday.
    """
    
    async def check(self, monitor: ContinuousMonitor) -> list[dict]:
        alerts = []
        
        for pos in monitor.portfolio.positions:
            # === PROFIT TARGET HIT INTRADAY ===
            if pos.profit_pct >= 0.50 and pos.days_to_expiry > 14:
                alerts.append({
                    "type": "profit_target",
                    "urgency": "medium",
                    "message": f"💰 {pos.symbol} {pos.strike}P hit 50% profit intraday. "
                               f"Close now to lock in ${pos.current_profit:.0f}?"
                })
            
            # === APPROACHING LOSS STOP ===
            loss_multiple = pos.current_price / pos.entry_price
            if loss_multiple >= 1.7:  # approaching 2x stop
                alerts.append({
                    "type": "loss_warning",
                    "urgency": "high",
                    "message": f"⚠️ {pos.symbol} {pos.strike}P at {loss_multiple:.1f}x entry. "
                               f"Approaching 2x loss stop. Watch closely."
                })
            
            # === LOSS STOP TRIGGERED ===
            max_mult = (1.5 if pos.days_to_expiry <= 10 else 2.0)
            if loss_multiple >= max_mult:
                alerts.append({
                    "type": "loss_stop",
                    "urgency": "critical",
                    "message": f"🛑 {pos.symbol} {pos.strike}P hit {max_mult}x loss stop. "
                               f"CLOSE NOW. Buy to close at market."
                })
            
            # === DELTA SHIFTED DANGEROUSLY ===
            if abs(pos.delta) > 0.50:  # option is basically ITM
                alerts.append({
                    "type": "delta_warning",
                    "urgency": "high",
                    "message": f"⚠️ {pos.symbol} {pos.strike}P delta at {pos.delta:.2f}. "
                               f"Essentially ITM. Roll or close today."
                })
            
            # === APPROACHING EXPIRY WITH RISK ===
            if pos.days_to_expiry <= 2 and abs(pos.delta) > 0.30:
                alerts.append({
                    "type": "expiry_risk",
                    "urgency": "critical",
                    "message": f"🛑 {pos.symbol} {pos.strike}P expires in {pos.days_to_expiry}d "
                               f"with delta {pos.delta:.2f}. Close or accept assignment."
                })
        
        return alerts
```

### News & Social Tripwires (2-minute loop)

```python
class NewsTripwires:
    """
    Lightweight news/social check that can trigger immediate analysis.
    Not the full Scout Agent pipeline — just fast-breaking headlines.
    """
    
    async def check(self, monitor: ContinuousMonitor) -> list[str]:
        tripped = []
        
        # Fast headline check from Benzinga or similar
        headlines = await get_recent_headlines(minutes=2)
        
        for headline in headlines:
            ticker = extract_ticker(headline.text)
            if not ticker or ticker not in monitor.watchlist:
                continue
            
            # Classify headline urgency
            urgency_keywords = {
                "high": ["downgrade", "cut", "miss", "crash", "halt", "warning",
                         "layoff", "recall", "investigation", "subpoena"],
                "medium": ["upgrade", "beat", "raised", "outperform", "initiated",
                           "acquisition", "merger", "dividend", "buyback"],
            }
            
            for urgency, keywords in urgency_keywords.items():
                if any(kw in headline.text.lower() for kw in keywords):
                    tripped.append(ticker)
                    break
        
        # Quick social buzz check (simpler than full Scout)
        trending = await get_trending_tickers(source="fintwit", minutes=15)
        for ticker, buzz_count in trending.items():
            if ticker in monitor.watchlist and buzz_count > 10:
                tripped.append(ticker)
        
        return list(set(tripped))
```

### Full Analysis Trigger

```python
async def on_tripwire(
    monitor: ContinuousMonitor,
    symbol: str,
    tripwire_type: str
):
    """
    When a tripwire fires, spin up the full analysis pipeline 
    on this specific ticker. If it qualifies, push an alert.
    
    This is the key insight: you're NOT running full analysis on 
    50 stocks every 30 seconds. You're running TRIPWIRES on 50 
    stocks every 30 seconds, and FULL ANALYSIS on 2-3 stocks 
    when something interesting happens.
    """
    
    # Rate limit: don't re-analyze the same ticker within 15 minutes
    last_analyzed = monitor.last_analysis_time.get(symbol)
    if last_analyzed and (datetime.now() - last_analyzed).seconds < 900:
        return
    
    # Full analysis (same pipeline as morning briefing, single ticker)
    mkt = await get_market_context(symbol)
    hist = await get_price_history(symbol)
    chain = await get_options_chain(symbol)
    cal = await get_event_calendar(symbol)
    
    # Run signal detection
    dip_signals = detect_dip_signals(symbol, mkt, hist)
    iv_signals = detect_iv_surface_signals(symbol, mkt, chain)
    all_signals = dip_signals + iv_signals
    
    if not all_signals:
        return  # tripwire fired but no tradeable signal
    
    # Check portfolio Greeks gate
    portfolio = monitor.portfolio
    smart_strikes = find_smart_strikes(symbol, chain, hist, "sell_put", get_trading_params())
    
    if not smart_strikes:
        return
    
    # Size the opportunity
    opp = build_opportunity(symbol, smart_strikes[0], all_signals, portfolio)
    sized = size_position(opp, all_signals, portfolio, get_trading_params())
    
    # Check Greeks guard
    allowed, reason = check_greeks_before_trade(sized, portfolio, get_greeks_targets(), get_regime())
    if not allowed:
        return
    
    # Qualified! Build and send alert
    monitor.last_analysis_time[symbol] = datetime.now()
    
    if len(monitor.alerts_sent_today) >= monitor.max_alerts_per_day:
        return  # daily alert cap reached
    
    alert = build_intraday_alert(symbol, sized, all_signals, tripwire_type, mkt)
    await send_telegram(alert)
    monitor.alerts_sent_today.append(symbol)


def build_intraday_alert(
    symbol: str,
    sized: SizedOpportunity,
    signals: list[AlphaSignal],
    tripwire_type: str,
    mkt: MarketContext
) -> str:
    """
    Intraday alert format — concise, actionable, with a 
    clear expiration time for the opportunity.
    """
    return f"""
⚡ INTRADAY SIGNAL: {symbol} ({tripwire_type})

{symbol} at ${mkt.price:.2f} ({mkt.price_change_1d:+.1f}% today)
IV Rank: {mkt.iv_rank:.0f} | RSI: {mkt.rsi_14:.0f if hasattr(mkt, 'rsi_14') else 'N/A'}

SIGNALS ({len(signals)} firing):
{chr(10).join(f'  • {s.signal_type.value}: {s.reasoning}' for s in signals[:3])}

TRADE:
  SELL {sized.contracts}x {symbol} {sized.strike}P {sized.expiration}
  Premium: ${sized.premium:.2f} | Yield: {sized.yield_on_capital:.1%}
  Conviction: {sized.conviction.upper()}
  Capital: ${sized.capital_deployed:,.0f} ({sized.portfolio_pct:.1%} of NLV)

EXECUTE IF: {symbol} between ${mkt.price * 0.97:.2f}-${mkt.price * 1.02:.2f}
EXPIRES: This alert is valid for 30 minutes.

[EXECUTE] [SKIP] [MODIFY]
"""
```

### Alert Priority & Throttling

```python
class AlertPriority(Enum):
    """
    Not every tripwire deserves an immediate notification.
    Priority determines delivery speed and notification style.
    """
    CRITICAL = "critical"    # immediate push: loss stops, expiry risk
    HIGH = "high"            # push within 1 min: new trade opportunities with 2+ signals
    MEDIUM = "medium"        # batch every 15 min: profit targets, single signals
    LOW = "low"              # include in post-market review: marginal setups


@dataclass
class AlertThrottling:
    """
    Prevent alert fatigue. A noisy system gets ignored — 
    which defeats the purpose.
    """
    max_alerts_per_day: int = 8         # total across all types
    max_alerts_per_hour: int = 3        # burst limit
    max_same_ticker_per_day: int = 2    # don't nag about the same stock
    
    # Quiet hours (no MEDIUM/LOW alerts)
    quiet_start: str = "11:30"          # lunch lull — thin volume
    quiet_end: str = "13:00"
    
    # Priority override: CRITICAL alerts always send
    critical_ignores_throttle: bool = True
    
    # Cooldown after execution
    post_trade_cooldown_minutes: int = 30  # don't suggest more trades right after one
```

### Continuous Monitor Config

```yaml
continuous_monitor:
  enabled: true
  market_hours_only: true
  
  # Polling intervals (seconds)
  price_poll: 30
  iv_poll: 300
  news_poll: 120
  portfolio_poll: 60
  
  # Tripwire thresholds
  price_drop_threshold: 0.025      # 2.5% intraday drop triggers analysis
  sudden_drop_threshold: 0.015     # 1.5% in 15 minutes
  volume_spike_threshold: 3.0      # 3x average volume
  iv_rank_change_threshold: 15     # 15-point IV rank move
  bounce_from_low_threshold: 0.01  # 1% bounce from intraday low
  
  # Alert limits
  max_alerts_per_day: 8
  max_alerts_per_hour: 3
  max_same_ticker_per_day: 2
  alert_valid_minutes: 30          # how long an intraday trade alert stays valid
  
  # Re-analysis cooldown
  reanalysis_cooldown_minutes: 15  # don't re-analyze same ticker within 15 min
  
  # Rate limiting (E*Trade)
  etrade_requests_per_second: 3    # stay under their 4/s limit
  batch_size: 25                   # E*Trade quote batch limit
  
  # After-hours
  post_market_review_time: "16:30"
  pre_market_briefing_time: "08:00"
```

### Resource Usage

```
Continuous monitor estimated API calls per trading day:

Price polling (30s × 6.5hrs):    780 batched calls (25 symbols each)
IV recalc (5min × 6.5hrs):       78 calls
News check (2min × 6.5hrs):     195 calls  
Portfolio refresh (1min):        390 calls
Full analysis (on tripwire):    ~20-40 calls (2-3 tickers × ~10 calls each)

Total: ~1,500 E*Trade API calls/day
E*Trade allows: 4/s × 23,400s = 93,600 calls/day max
Usage: ~1.6% of limit — plenty of headroom

Claude API (for full analysis):  ~5-10 calls/day at $0.01-0.03 each
Estimated daily cost: $0.10-0.30 in Claude API
```

### Post-Market Review (4:30 PM)

```python
async def generate_post_market_review(
    monitor: ContinuousMonitor,
    trades_today: list[TradeRecord]
) -> str:
    """
    End-of-day summary that sets up tomorrow's morning briefing.
    
    Answers:
    - What signals fired today? How many led to trades?
    - Which positions changed status?
    - What's the overnight risk profile?
    - What morning briefing trades are still valid for tomorrow?
    - Any earnings/events tomorrow to prepare for?
    """
    
    return f"""
━━ POST-MARKET REVIEW — {date.today()} ━━

TODAY'S ACTIVITY:
  Tripwires fired: {len(monitor.tripwires_fired_today)}
  Full analyses run: {len(monitor.analyses_run_today)}
  Alerts sent: {len(monitor.alerts_sent_today)}
  Trades executed: {len(trades_today)}

SIGNALS THAT FIRED:
{format_daily_signals(monitor.signals_detected_today)}

POSITION STATUS CHANGES:
{format_position_changes(monitor.position_changes_today)}

P&L TODAY:
  Realized: ${sum(t.pnl for t in trades_today):+,.0f}
  Unrealized change: ${monitor.unrealized_change_today:+,.0f}
  Portfolio: ${monitor.portfolio.net_liquidation:,.0f} ({monitor.daily_return:+.2%})

OVERNIGHT RISK:
  Open positions: {len(monitor.portfolio.positions)}
  Portfolio delta: {monitor.portfolio.portfolio_beta_delta:.0f} SPY-eq
  Largest single risk: {monitor.largest_risk_position}
  Earnings tomorrow: {', '.join(monitor.earnings_tomorrow) or 'None'}

CARRY FORWARD TO TOMORROW:
{format_carry_forward(monitor.still_valid_opportunities)}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
```

### Multi-Cycle Portfolio Analysis Schedule

The system doesn't analyze your portfolio once a day. It runs FULL portfolio-level 
analysis at 5 scheduled checkpoints plus continuous tripwire monitoring between them.

Individual position tripwires catch micro-events (single stock dip, profit target hit).
Portfolio-level analysis catches MACRO changes that individual monitoring misses: 
aggregate delta drifting, concentration shifting because one stock moved 8%, margin 
utilization creeping up, regime changing mid-day, new correlation clusters forming.

```
DAILY ANALYSIS SCHEDULE ($1M ACCOUNT)

 7:00 AM   DATA COLLECTION
           Pull pre-market prices, overnight news, futures, 
           Asia/Europe market data, pre-market movers.

 8:00 AM   📊 FULL PORTFOLIO ANALYSIS #1 — MORNING BRIEFING
           Complete analysis: all 19 system components.
           Portfolio allocation check, signal scan, opportunities,
           regime classification, risk assessment.
           OUTPUT: Morning briefing via Telegram.

 9:30 AM   Market open → Continuous Monitor starts
           Tripwires: price (30s), IV (5min), positions (1min), news (2min)

10:30 AM   📊 FULL PORTFOLIO ANALYSIS #2 — POST-OPENING ASSESSMENT
           The first hour is chaos. By 10:30 the picture is clearer.
           - Did any morning briefing trades get invalidated by the open?
           - Did the opening create new dip opportunities?
           - Has the regime changed based on market open direction?
           - Update portfolio Greeks with live mid-day data.
           - Re-score any pending opportunities with live prices.
           OUTPUT: "Post-Open Update" push if anything changed materially.

10:30-1:00 Continuous monitoring (tripwires only)

 1:00 PM   📊 FULL PORTFOLIO ANALYSIS #3 — MIDDAY CHECK
           The midday lull is when you reassess the day's thesis.
           - Portfolio delta: has it drifted outside regime targets?
           - Concentration: did one stock's 5% move change the math?
           - Margin utilization: approaching any limits?
           - Theta decay checkpoint: on track for the day?
           - Any new signals fired since morning on watchlist names?
           - FOMC/CPI/macro event in the next 24 hours?
           OUTPUT: "Midday Status" push only if action needed.
           (If everything's fine, stay silent — don't spam.)

 1:00-3:30 Continuous monitoring (tripwires only)

 3:30 PM   📊 FULL PORTFOLIO ANALYSIS #4 — END-OF-DAY ASSESSMENT
           The final 30 minutes matter for several reasons:
           - Positions expiring THIS WEEK: any at risk of pin?
           - Positions approaching profit targets: close before EOD?
           - Any earnings TONIGHT on names you hold?
           - VIX term structure: overnight risk assessment.
           - Capital freed today: queue for tomorrow's deployment?
           - Margin: will overnight requirement cause issues?
           OUTPUT: "EOD Actions" push with specific close-before-4pm trades.

 4:00 PM   Market close → Continuous monitor stops.

 4:30 PM   📊 FULL PORTFOLIO ANALYSIS #5 — POST-MARKET REVIEW
           Daily summary: signals fired, trades executed, P&L,
           position status changes, overnight risk profile.
           Carry forward: which opportunities are still valid tomorrow?
           Feed into tomorrow's morning briefing data.
           OUTPUT: "Daily Review" push.

TOTAL FULL ANALYSES PER DAY: 5
TOTAL ALERTS: typically 3-8 (morning briefing + 1-3 intraday + EOD)
SILENT WHEN NOTHING MATTERS: midday check suppresses alert if no action needed.
```

```python
@dataclass
class PortfolioAnalysisCycle:
    """A single full portfolio analysis cycle."""
    
    cycle_name: str           # "morning", "post_open", "midday", "eod", "post_market"
    scheduled_time: str       # "08:00", "10:30", "13:00", "15:30", "16:30"
    
    # What this cycle produces
    outputs: list[str]        # ["briefing", "alerts", "silent_if_ok"]
    
    # What triggers an alert from this cycle
    alert_conditions: list[str]
    
    # Whether to push even if nothing changed
    always_push: bool         # True for morning/post-market, False for midday


ANALYSIS_CYCLES = [
    PortfolioAnalysisCycle(
        cycle_name="morning",
        scheduled_time="08:00",
        outputs=["full_briefing", "trade_proposals", "allocation_check"],
        alert_conditions=["always"],
        always_push=True
    ),
    PortfolioAnalysisCycle(
        cycle_name="post_open",
        scheduled_time="10:30",
        outputs=["opportunity_update", "regime_check", "greeks_update"],
        alert_conditions=[
            "new_dip_signal_fired",
            "morning_trade_invalidated", 
            "regime_changed",
            "margin_utilization_above_40pct"
        ],
        always_push=False   # silent if nothing changed
    ),
    PortfolioAnalysisCycle(
        cycle_name="midday",
        scheduled_time="13:00",
        outputs=["portfolio_health", "delta_drift", "concentration_check"],
        alert_conditions=[
            "portfolio_delta_outside_range",
            "concentration_violation_new",
            "margin_approaching_limit",
            "new_high_conviction_signal"
        ],
        always_push=False   # silent if portfolio is healthy
    ),
    PortfolioAnalysisCycle(
        cycle_name="eod",
        scheduled_time="15:30",
        outputs=["expiry_risk", "close_before_eod", "overnight_assessment"],
        alert_conditions=[
            "position_expiring_this_week_at_risk",
            "earnings_tonight_on_open_position",
            "close_winner_before_eod",
            "margin_overnight_concern"
        ],
        always_push=False   # push only if action needed before 4pm
    ),
    PortfolioAnalysisCycle(
        cycle_name="post_market",
        scheduled_time="16:30",
        outputs=["daily_review", "pnl_summary", "carry_forward", "tax_impact"],
        alert_conditions=["always"],
        always_push=True
    ),
]


async def run_analysis_cycle(cycle: PortfolioAnalysisCycle):
    """
    Run a full portfolio analysis for the given cycle.
    The analysis is the same depth every time — what changes is 
    WHAT we alert on and WHETHER we push a notification.
    """
    
    # Full data refresh
    portfolio = await refresh_portfolio()
    market_data = await refresh_all_market_data()
    events = await refresh_events()
    
    # Full analysis pipeline
    signals = detect_all_signals(portfolio, market_data)
    opportunities = find_and_rank_opportunities(portfolio, market_data, signals)
    risk = calculate_risk(portfolio, market_data)
    greeks_status = check_portfolio_greeks(portfolio)
    regime = classify_regime(market_data)
    allocation = check_allocation(portfolio)
    tax_alerts = check_tax_implications(portfolio)
    wash_sale_blocks = check_wash_sales()
    
    # Determine if alert is needed for this cycle
    should_alert = cycle.always_push
    alert_reasons = []
    
    for condition in cycle.alert_conditions:
        if condition == "always":
            should_alert = True
            break
        if evaluate_condition(condition, portfolio, signals, risk, regime):
            should_alert = True
            alert_reasons.append(condition)
    
    if should_alert:
        briefing = await generate_cycle_briefing(
            cycle, portfolio, signals, opportunities, risk, 
            greeks_status, regime, allocation, tax_alerts
        )
        await send_telegram(briefing)
    
    # Always log the snapshot regardless of whether we alerted
    await log_analysis_snapshot(cycle, portfolio, signals, risk)
```

---

## Comprehensive Tax Framework

Options trading on a $1M account generates significant tax events.
Without tracking, you'll overpay by thousands annually. The system 
must be tax-aware on every trade, every day.

### Tax Engine

```python
@dataclass
class TaxEngine:
    """
    Tracks tax implications across the entire portfolio in real-time.
    Every trade proposal includes its estimated tax cost.
    Every daily review includes a running tax liability estimate.
    """
    
    # Federal tax rates (adjust for your bracket)
    short_term_rate: float = 0.37       # ordinary income rate
    long_term_rate: float = 0.20        # LTCG rate
    niit_rate: float = 0.038            # Net Investment Income Tax (>$250K AGI)
    state_rate: float = 0.00            # set for your state (0 for TX/FL/WA/etc.)
    
    # Combined effective rates
    @property
    def stcg_effective(self) -> float:
        return self.short_term_rate + self.niit_rate + self.state_rate
    
    @property
    def ltcg_effective(self) -> float:
        return self.long_term_rate + self.niit_rate + self.state_rate
    
    # Running tallies (reset annually)
    realized_stcg_ytd: float = 0.0      # short-term capital gains
    realized_ltcg_ytd: float = 0.0      # long-term capital gains
    realized_losses_ytd: float = 0.0    # capital losses
    option_premium_income_ytd: float = 0.0  # always STCG
    
    # Tax-loss harvesting bank
    harvested_losses_ytd: float = 0.0   # losses available to offset gains
    remaining_loss_carryforward: float = 0.0  # from prior years
    
    # Wash sale tracking
    wash_sale_tracker: WashSaleTracker = None


@dataclass
class TradeTaxImpact:
    """
    Estimated tax impact for a single trade.
    Shown in every trade proposal and execution confirmation.
    """
    trade_description: str
    gross_pnl: float
    
    # Classification
    is_short_term: bool           # options are always short-term
    holding_period_days: int
    
    # Tax calculation
    tax_rate: float               # effective rate applied
    estimated_tax: float          # tax owed on this trade
    net_after_tax: float          # what you actually keep
    
    # Context
    wash_sale_risk: bool          # would this trigger a wash sale?
    wash_sale_ticker: str | None
    wash_sale_warning: str | None
    
    # Optimization opportunities
    can_offset_with_losses: bool  # existing harvested losses available?
    loss_offset_amount: float     # how much loss bank offsets this gain
    net_tax_after_offset: float   # tax after applying loss offsets


def calculate_trade_tax_impact(
    trade: SizedOpportunity,
    tax_engine: TaxEngine,
    is_closing: bool = False,
    entry_price: float = 0,
    exit_price: float = 0
) -> TradeTaxImpact:
    """
    Calculate tax impact for any trade — opening or closing.
    
    Key tax rules for options:
    - Short option premium is NOT taxable at receipt. It's taxable when
      the option expires, is closed, or you're assigned.
    - ALL option gains are short-term (regardless of holding period).
    - If assigned on a short put, the premium REDUCES your cost basis
      on the stock. The stock then starts its own holding period.
    - If your short call is assigned, the premium is ADDED to the 
      sale price of the stock. This can convert STCG to LTCG if 
      the stock was held >1 year.
    """
    
    if not is_closing:
        # Opening a new position — no immediate tax event
        # But check wash sale risk
        wash_ok, wash_warning = tax_engine.wash_sale_tracker.check_before_trade(trade.symbol)
        
        return TradeTaxImpact(
            trade_description=f"OPEN: {trade.symbol} {trade.strike}P",
            gross_pnl=0,
            is_short_term=True,
            holding_period_days=0,
            tax_rate=0,
            estimated_tax=0,
            net_after_tax=0,
            wash_sale_risk=not wash_ok,
            wash_sale_ticker=trade.symbol if not wash_ok else None,
            wash_sale_warning=wash_warning,
            can_offset_with_losses=False,
            loss_offset_amount=0,
            net_tax_after_offset=0,
        )
    
    # Closing a position — taxable event
    gross_pnl = (entry_price - exit_price) * trade.contracts * 100  # for short options
    
    if gross_pnl > 0:
        # Winning trade — check if losses can offset
        available_offset = min(
            tax_engine.harvested_losses_ytd + tax_engine.remaining_loss_carryforward,
            gross_pnl
        )
        taxable_gain = gross_pnl - available_offset
        tax_rate = tax_engine.stcg_effective  # options always short-term
        estimated_tax = taxable_gain * tax_rate
        
        return TradeTaxImpact(
            trade_description=f"CLOSE: {trade.symbol} — GAIN",
            gross_pnl=gross_pnl,
            is_short_term=True,
            holding_period_days=0,
            tax_rate=tax_rate,
            estimated_tax=estimated_tax,
            net_after_tax=gross_pnl - estimated_tax,
            wash_sale_risk=False,
            wash_sale_ticker=None,
            wash_sale_warning=None,
            can_offset_with_losses=available_offset > 0,
            loss_offset_amount=available_offset,
            net_tax_after_offset=estimated_tax,
        )
    else:
        # Losing trade — generates a tax loss (unless wash sale)
        wash_ok, wash_warning = tax_engine.wash_sale_tracker.check_before_trade(trade.symbol)
        
        return TradeTaxImpact(
            trade_description=f"CLOSE: {trade.symbol} — LOSS",
            gross_pnl=gross_pnl,
            is_short_term=True,
            holding_period_days=0,
            tax_rate=0,
            estimated_tax=0,
            net_after_tax=gross_pnl,  # loss
            wash_sale_risk=not wash_ok,
            wash_sale_ticker=trade.symbol if not wash_ok else None,
            wash_sale_warning=(
                f"This loss of ${abs(gross_pnl):,.0f} will be DISALLOWED if you "
                f"open a new {trade.symbol} position within 30 days. "
                f"The loss gets added to the cost basis of the new position."
                if not wash_ok else None
            ),
            can_offset_with_losses=False,
            loss_offset_amount=abs(gross_pnl),  # this IS the loss to bank
            net_tax_after_offset=0,
        )
```

### Assignment Tax Rules

```python
def calculate_assignment_tax(
    assigned_put: Position,
    current_stock_price: float
) -> str:
    """
    When assigned on a short put, the tax rules are specific:
    
    1. The premium you received is NOT a separate taxable event.
    2. Instead, the premium REDUCES your cost basis on the stock.
    3. Your holding period for the stock starts on assignment date.
    4. Capital gains tax is determined when you SELL the stock.
    
    Example:
    - Sold NVDA 95P for $4.50 premium
    - Assigned at $95 (stock dropped below $95)
    - Cost basis = $95 - $4.50 = $90.50 per share
    - If you sell at $100: gain = $100 - $90.50 = $9.50/share
    - Holding period starts at assignment date
    """
    premium_received = assigned_put.entry_price
    assignment_price = assigned_put.strike
    cost_basis = assignment_price - premium_received
    
    unrealized_gain = current_stock_price - cost_basis
    
    return f"""
TAX IMPACT OF ASSIGNMENT: {assigned_put.symbol}

Put premium received: ${premium_received:.2f}/share
Assignment price:     ${assignment_price:.2f}/share
Cost basis:           ${cost_basis:.2f}/share (strike - premium)
Current stock price:  ${current_stock_price:.2f}/share
Unrealized gain:      ${unrealized_gain:.2f}/share

Holding period starts: {date.today()} (assignment date)
LTCG eligible:        {date.today() + timedelta(days=365)}

COVERED CALL TAX INTERACTION:
If you sell a covered call and get assigned (called away):
  Sale price = call strike + call premium received
  Gain = sale price - cost basis (${cost_basis:.2f})
  
  If stock held >1 year at time of call assignment → LTCG
  If stock held <1 year → STCG
  
  ⚠️ DEEP ITM COVERED CALLS can reset your holding period.
  Only sell OTM or slightly ITM calls to preserve LTCG eligibility.
"""
```

### Account-Type Tax Optimization + Liquidity Constraints

```python
@dataclass
class BrokerageAccount:
    """Represents one brokerage account with its constraints."""
    account_id: str
    account_type: str           # "taxable", "roth_ira", "traditional_ira", "401k_rollover"
    
    # Balances
    total_value: float
    cash_available: float
    buying_power: float         # includes margin for taxable
    
    # Options permissions (E*Trade levels)
    # Level 1: Covered calls, protective puts
    # Level 2: Level 1 + cash-secured puts, long options
    # Level 3: Level 2 + spreads
    # Level 4: Level 3 + naked options, strangles
    options_level: int
    
    # Restrictions
    margin_enabled: bool         # IRAs: False (no margin)
    can_short_stock: bool        # IRAs: False
    
    # Liquidity
    withdrawal_restricted: bool  # True for IRAs if under 59.5
    early_withdrawal_penalty: float  # 0.10 for traditional IRA, 0 for Roth contributions
    
    # Contribution limits
    annual_contribution_limit: float  # $7K for IRA (2026), $0 if maxed
    contributions_this_year: float
    remaining_contribution_room: float
    
    # Roth-specific
    roth_contribution_basis: float  # total contributions (withdrawable anytime)
    roth_earnings: float            # growth (locked until 59.5)
    
    @property
    def liquid_value(self) -> float:
        """How much can be withdrawn without penalty."""
        if self.account_type == "taxable":
            return self.total_value  # fully liquid
        elif self.account_type == "roth_ira":
            return self.roth_contribution_basis  # contributions only
        else:
            return 0  # traditional IRA / 401k fully locked
    
    @property
    def allowed_strategies(self) -> list[str]:
        """What strategies this account can execute based on options level."""
        strategies = []
        if self.options_level >= 1:
            strategies.extend(["covered_call", "protective_put"])
        if self.options_level >= 2:
            strategies.extend(["monthly_put", "weekly_put", "dividend_capture"])
        if self.options_level >= 3:
            strategies.extend(["put_spread", "call_spread"])
        if self.options_level >= 4:
            strategies.extend(["strangle", "naked_put", "earnings_crush"])
        return strategies


@dataclass
class AccountRouter:
    """
    Route trades to the optimal account for tax efficiency
    while respecting liquidity constraints and options restrictions.
    
    ROUTING HIERARCHY:
    1. Can this account physically execute this strategy? (options level)
    2. Does this account have enough buying power?
    3. Would routing here violate liquidity requirements?
    4. What's the tax-optimal placement?
    
    ACCOUNT TYPE GUIDE:
    
    TAXABLE BROKERAGE (fully liquid):
    ✅ Engine 1 long-term holds (LTCG treatment when sold)
    ✅ Tax-loss harvesting (need realized losses in taxable)
    ✅ Strangles, spreads, earnings crush (need Level 3-4)
    ✅ Any strategy that needs margin
    ✅ Anything you might need to access as cash
    ❌ High-frequency premium income (taxed at 37%)
    
    ROTH IRA (contributions liquid, earnings locked):
    ✅ Cash-secured puts and covered calls (Level 2)
    ✅ Weekly puts, monthly puts (tax-FREE income!)
    ✅ Highest-frequency strategies (max tax savings)
    ❌ Strangles (usually requires Level 4, not available in IRA)
    ❌ Put spreads (may require Level 3, check your IRA level)
    ❌ Strategies requiring margin
    ❌ Money you might need before 59.5
    
    TRADITIONAL IRA (fully locked):
    ✅ Same strategies as Roth (Level 1-2 typically)
    ✅ Bond/REIT income (taxed at ordinary rates anyway)
    ❌ Everything locked until 59.5 + taxed on withdrawal
    ❌ Less attractive than Roth for premium income
    """
    
    accounts: dict[str, BrokerageAccount]
    
    # Liquidity requirements
    min_liquid_pct: float = 0.60        # at least 60% of total NLV must be liquid
    min_liquid_dollars: float = 100_000  # absolute minimum liquid cash
    emergency_reserve_months: int = 6    # 6 months expenses always accessible
    monthly_expenses: float = 10_000     # set during onboarding
    
    @property
    def total_nlv(self) -> float:
        return sum(a.total_value for a in self.accounts.values())
    
    @property
    def total_liquid(self) -> float:
        return sum(a.liquid_value for a in self.accounts.values())
    
    @property
    def liquidity_ratio(self) -> float:
        return self.total_liquid / self.total_nlv if self.total_nlv else 0
    
    def recommend_account(self, trade: SizedOpportunity) -> tuple[str, str]:
        """
        Given a trade, recommend which account to place it in.
        Returns (account_id, reasoning).
        """
        
        # Step 1: Filter accounts that CAN execute this strategy
        eligible = []
        for acct_id, acct in self.accounts.items():
            strategy_name = trade.strategy or "monthly_put"
            if strategy_name not in acct.allowed_strategies:
                continue
            if acct.buying_power < trade.capital_deployed:
                continue
            # Margin check: IRAs are cash-only
            if not acct.margin_enabled and trade.requires_margin:
                continue
            eligible.append(acct)
        
        if not eligible:
            return (self.get_taxable_account().account_id,
                    "Only taxable account has sufficient permissions and buying power.")
        
        # Step 2: Check liquidity constraint
        # Would routing to a restricted account push liquid ratio below minimum?
        for acct in eligible:
            if acct.withdrawal_restricted:
                # Adding capital to restricted account reduces liquidity
                new_liquid_ratio = (self.total_liquid) / (self.total_nlv)
                # The capital is already in the account — we're just deploying it
                # Liquidity concern is if the ACCOUNT BALANCE is growing via 
                # contributions or reinvestment while taxable shrinks
                pass
        
        # Step 3: Tax-optimal routing
        # Priority: Roth (tax-free) > Traditional IRA (tax-deferred) > Taxable (taxed now)
        
        # High-frequency premium selling → Roth first
        if trade.strategy in ("weekly_put", "monthly_put", "earnings_crush"):
            roth = self.get_account_by_type("roth_ira")
            if roth and roth in eligible:
                estimated_tax_saved = trade.premium * trade.contracts * 100 * 0.37
                return (roth.account_id,
                        f"Routed to Roth IRA — premium income is tax-FREE. "
                        f"Saves ~${estimated_tax_saved:.0f} in taxes on this trade.")
        
        # Strangles and spreads → must be taxable (IRA restrictions)
        if trade.strategy in ("strangle", "earnings_crush", "put_spread"):
            taxable = self.get_account_by_type("taxable")
            if taxable and taxable in eligible:
                return (taxable.account_id,
                        f"Routed to taxable — {trade.strategy} requires options Level 3+, "
                        f"not available in IRA accounts.")
        
        # Engine 1 long-term equity → taxable (for LTCG treatment)
        if trade.trade_type == "buy_stock" and trade.engine == "engine1":
            taxable = self.get_account_by_type("taxable")
            if taxable:
                return (taxable.account_id,
                        "Routed to taxable — long-term holds get LTCG treatment (20% vs 37%).")
        
        # Default: taxable (most flexible)
        taxable = self.get_account_by_type("taxable")
        return (taxable.account_id, "Default routing to taxable account.")
    
    def check_liquidity_health(self) -> tuple[bool, str]:
        """
        Run in every portfolio analysis cycle.
        Alerts if too much capital is locked in restricted accounts.
        """
        liquid = self.total_liquid
        nlv = self.total_nlv
        ratio = self.liquidity_ratio
        emergency_reserve = self.monthly_expenses * self.emergency_reserve_months
        
        alerts = []
        healthy = True
        
        if ratio < self.min_liquid_pct:
            healthy = False
            alerts.append(
                f"⚠️ LIQUIDITY: Only {ratio:.0%} of NLV is liquid "
                f"(target ≥{self.min_liquid_pct:.0%}). "
                f"${liquid:,.0f} accessible out of ${nlv:,.0f} total. "
                f"Avoid adding more capital to IRA accounts."
            )
        
        if liquid < self.min_liquid_dollars:
            healthy = False
            alerts.append(
                f"🛑 LIQUIDITY: Liquid assets ${liquid:,.0f} below "
                f"${self.min_liquid_dollars:,.0f} minimum. "
                f"Do NOT deploy more taxable capital into options. "
                f"Build cash position first."
            )
        
        if liquid < emergency_reserve:
            healthy = False
            alerts.append(
                f"🛑 EMERGENCY RESERVE: Liquid assets ${liquid:,.0f} below "
                f"{self.emergency_reserve_months}-month reserve "
                f"(${emergency_reserve:,.0f}). Priority: build cash."
            )
        
        # Check if IRA is growing disproportionately from reinvested gains
        for acct in self.accounts.values():
            if acct.withdrawal_restricted:
                acct_pct = acct.total_value / nlv
                if acct_pct > 0.40:
                    alerts.append(
                        f"⚠️ {acct.account_type.upper()} is {acct_pct:.0%} of total NLV. "
                        f"Consider routing new premium income to taxable instead "
                        f"to maintain liquidity balance."
                    )
        
        return (healthy, "\n".join(alerts) if alerts else "✅ Liquidity healthy.")
    
    def get_account_by_type(self, acct_type: str) -> BrokerageAccount | None:
        for acct in self.accounts.values():
            if acct.account_type == acct_type:
                return acct
        return None
    
    def get_taxable_account(self) -> BrokerageAccount:
        return self.get_account_by_type("taxable")
    
    def estimate_annual_tax_savings(self) -> str:
        """
        Estimate how much tax is saved by routing to Roth vs all-taxable.
        Shown in onboarding and quarterly reviews.
        """
        roth = self.get_account_by_type("roth_ira")
        if not roth:
            return "No Roth IRA available. All income taxed at ordinary rates."
        
        # Estimate: Roth buying power × expected annual yield × tax rate
        roth_capacity = roth.buying_power
        estimated_annual_yield = 0.40  # 40% target
        estimated_premium_in_roth = roth_capacity * estimated_annual_yield
        tax_saved = estimated_premium_in_roth * 0.37  # STCG rate
        
        return f"""
ANNUAL TAX SAVINGS FROM ROTH ROUTING:
  Roth buying power: ${roth_capacity:,.0f}
  Estimated annual premium in Roth: ${estimated_premium_in_roth:,.0f}
  Tax saved (vs taxable at 37%): ${tax_saved:,.0f}/year
  
  That's ${tax_saved/12:,.0f}/month you keep instead of sending to IRS.
  Over 10 years at 7% growth: ${tax_saved * 14.78:,.0f} in additional wealth.
"""
    
    def generate_routing_summary(self) -> str:
        """Show current account allocation and routing rules."""
        lines = ["━━ ACCOUNT ROUTING ━━\n"]
        
        for acct_id, acct in self.accounts.items():
            liquid_label = "LIQUID" if not acct.withdrawal_restricted else "LOCKED"
            strategies = ", ".join(acct.allowed_strategies[:4])
            lines.append(
                f"  {acct.account_type.upper()}: ${acct.total_value:,.0f} "
                f"({acct.total_value/self.total_nlv:.0%} of NLV) [{liquid_label}]\n"
                f"    Options Level {acct.options_level}: {strategies}\n"
                f"    Buying power: ${acct.buying_power:,.0f}\n"
            )
        
        lines.append(f"\n  Liquidity ratio: {self.liquidity_ratio:.0%} "
                     f"(target ≥{self.min_liquid_pct:.0%})")
        lines.append(f"  Emergency reserve: ${self.monthly_expenses * self.emergency_reserve_months:,.0f} "
                     f"({self.emergency_reserve_months} months × ${self.monthly_expenses:,.0f}/mo)")
        
        return "\n".join(lines)
```

### Tax-Aware Daily Alerts

```python
def generate_tax_section_for_briefing(
    tax_engine: TaxEngine,
    portfolio: PortfolioState,
    trades_proposed: list[SizedOpportunity]
) -> str:
    """
    Tax section that appears in every morning briefing and post-market review.
    """
    
    alerts = []
    
    # === YTD TAX LIABILITY ===
    stcg_tax = tax_engine.realized_stcg_ytd * tax_engine.stcg_effective
    ltcg_tax = tax_engine.realized_ltcg_ytd * tax_engine.ltcg_effective
    loss_offset = min(tax_engine.harvested_losses_ytd, 
                      tax_engine.realized_stcg_ytd + tax_engine.realized_ltcg_ytd)
    net_tax = stcg_tax + ltcg_tax - (loss_offset * tax_engine.stcg_effective)
    
    # === WASH SALE BLOCKS ===
    blocked = tax_engine.wash_sale_tracker.get_blocked_tickers()
    if blocked:
        alerts.append(
            f"🚫 WASH SALE BLOCKED: {', '.join(blocked)}. "
            f"No new positions until windows expire."
        )
    
    # Check proposed trades for wash sale risk
    for trade in trades_proposed:
        impact = calculate_trade_tax_impact(trade, tax_engine)
        if impact.wash_sale_risk:
            alerts.append(
                f"⚠️ {trade.symbol}: wash sale window active. "
                f"{impact.wash_sale_warning}"
            )
    
    # === LTCG APPROACHING ===
    for pos in portfolio.stock_positions:
        days_to_ltcg = 365 - pos.holding_period_days
        if 0 < days_to_ltcg <= 60 and pos.unrealized_gain > 5000:
            alerts.append(
                f"⏰ {pos.symbol}: ${pos.unrealized_gain:,.0f} unrealized gain "
                f"becomes LTCG in {days_to_ltcg} days. "
                f"Tax savings if you wait: ${pos.unrealized_gain * (tax_engine.stcg_effective - tax_engine.ltcg_effective):,.0f}. "
                f"Do NOT sell or get assigned before then."
            )
    
    # === TAX-LOSS HARVESTING OPPORTUNITIES ===
    for pos in portfolio.stock_positions:
        if pos.unrealized_gain < -3000:
            # Check wash sale — can we harvest?
            wash_ok, _ = tax_engine.wash_sale_tracker.check_before_trade(pos.symbol)
            if wash_ok:
                tax_saved = abs(pos.unrealized_gain) * tax_engine.stcg_effective
                alerts.append(
                    f"📉 TAX HARVEST: {pos.symbol} has ${abs(pos.unrealized_gain):,.0f} "
                    f"unrealized loss. Harvesting saves ~${tax_saved:,.0f} in taxes. "
                    f"Sell, wait 31 days (wash sale), re-enter via put sale. "
                    f"Or swap into a correlated name (e.g., NVDA → AMD) immediately."
                )
    
    # === QUARTERLY TAX ESTIMATE ===
    quarterly_estimate = net_tax / 4 if net_tax > 0 else 0
    next_estimated_payment = get_next_quarterly_tax_date()
    
    return f"""
━━ TAX DASHBOARD ━━
YTD Realized STCG:    ${tax_engine.realized_stcg_ytd:+,.0f} (taxed at {tax_engine.stcg_effective:.0%})
YTD Realized LTCG:    ${tax_engine.realized_ltcg_ytd:+,.0f} (taxed at {tax_engine.ltcg_effective:.0%})
YTD Harvested Losses: ${tax_engine.harvested_losses_ytd:,.0f} (offsetting gains)
Option Premium YTD:   ${tax_engine.option_premium_income_ytd:,.0f}

Estimated YTD Tax:    ${net_tax:,.0f}
Next quarterly est:   ${quarterly_estimate:,.0f} (due {next_estimated_payment})

{chr(10).join(alerts) if alerts else '✅ No tax alerts.'}
"""
```

### Tax SQL Schema

```sql
CREATE TABLE tax_events (
    id SERIAL PRIMARY KEY,
    trade_id INT REFERENCES paper_trades(id),
    event_date DATE NOT NULL,
    symbol VARCHAR(10) NOT NULL,
    event_type VARCHAR(30) NOT NULL,     -- 'option_close', 'stock_sale', 'assignment', 
                                          -- 'expiration', 'tax_loss_harvest'
    gross_pnl DECIMAL(12,2),
    is_short_term BOOLEAN,
    holding_period_days INT,
    tax_rate DECIMAL(5,4),
    estimated_tax DECIMAL(12,2),
    loss_offset_applied DECIMAL(12,2),
    wash_sale_triggered BOOLEAN DEFAULT FALSE,
    wash_sale_disallowed_loss DECIMAL(12,2),
    account_type VARCHAR(20),            -- 'taxable', 'roth', 'traditional_ira'
    notes TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE quarterly_tax_estimates (
    id SERIAL PRIMARY KEY,
    quarter VARCHAR(10) NOT NULL,        -- '2026-Q1', '2026-Q2', etc.
    estimated_stcg DECIMAL(12,2),
    estimated_ltcg DECIMAL(12,2),
    estimated_losses DECIMAL(12,2),
    estimated_tax_owed DECIMAL(12,2),
    payment_due_date DATE,
    payment_made BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW()
);
```

---

## Updated Morning Briefing Format

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHEEL COPILOT — Monday, April 13, 2026
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

REGIME: ATTACK 🔥 | VIX 24.8 (+3.2)

━━ PORTFOLIO ALLOCATION ━━
Engine 1 (Core Holdings): 47% ($354K) — target 50% ↑
Engine 2 (Active Wheel):  44% ($330K) — target 45% ✅  
Engine 3 (Dry Powder):     9% ($68K)  — target 5% ↓ deploy!

Monthly return: +3.8% ($28.5K) | YTD: +14.2%
Engine 2 ann. pace: 43% ✅ on target

━━ ACCOUNTS & LIQUIDITY ━━
Taxable:  $612K (61%) [LIQUID] — Level 4, margin enabled
Roth IRA: $320K (32%) [LOCKED*] — Level 2, CSPs + CCs only
Trad IRA:  $68K  (7%) [LOCKED]  — Level 2
*Roth: $180K contributions withdrawable, $140K earnings locked

Liquidity: 61% ✅ (target ≥60%)
Emergency reserve: $60K / $60K ✅

Today's routing:
  NVDA put → Roth (saves ~$540 in taxes)
  AVGO put → Roth (saves ~$420)
  AMD strangle → Taxable (needs Level 4)

━━ TAX ALERTS ━━
🚫 PLTR: wash sale window (closes Apr 28). No new PLTR trades.
⏰ GOOG: LTCG in 47 days. Do NOT sell calls risking assignment.
📉 COIN: $4,200 unrealized loss — harvest to offset $15K STCG?
YTD estimated tax: $38,400 | Next quarterly: $9,600 (due Jun 15)

━━ ADVISOR NOTES ━━
• AMZN -8% this week. HIGHEST conviction compounder at support.
  → SPLIT ENTRY: Buy 10 shares ($1,870) for Engine 1 +
    Sell 2x May 175P for Engine 2. Best of both worlds.
• ⏰ TAX: GOOG shares reach LTCG in 47 days (May 30).
  Do NOT sell calls that risk assignment. Tax savings: $4,200.
• ADBE: ESPP vest May 1 (~$12K). Plan: sell immediately,
  $4.8K → Engine 1 (buy AVGO), $6K → Engine 2, $1.2K → powder.
• NVDA assigned last week. Route: 60 shares Engine 1 (uncovered),
  40 shares Engine 2 (sell 0.15 delta calls — protect upside).

━━ SIGNAL FLASH ━━
🔴 NVDA -4.2% | RSI 28 | 200 SMA | IV rank 78 → HIGH CONVICTION
🔴 AVGO -3.1% | 4 red days | 50 SMA | IV rank 71 → HIGH CONVICTION
🟡 AMD -1.8% | IV rank 55 → MEDIUM
🆕 SCOUT: CRWD buzzing (3 sources, 72/100 buzz) — downgrade
   overreaction, IV rank 81 → VALIDATING

━━ ATTACK PLAN ━━
[... sized trades as before ...]

━━ POSITION MANAGEMENT ━━
[... closes, rolls, reloads ...]

━━ PORTFOLIO SCORECARD ━━
Daily theta:        +$224 ($6,720/month)
Capital efficiency: $0.44 theta per $100 deployed
Idle capital:       9% → deploying 4% today
Effective positions: 11 (correlation-adjusted from 25 names)
SPY beta:           1.35 — slightly high, consider hedge
Slippage MTD:       -$142 (0.8% of premium — acceptable)

━━ PERFORMANCE ATTRIBUTION (MTD) ━━
Monthly puts:    +$4,200 (62% win rate)
Weekly dip puts: +$2,800 (71% win rate) ← best performer
Strangles:       +$1,900 (58% win rate)
Earnings crush:  +$1,100 (2 trades, both winners)
Scout picks:     +$890 (3/4 winners) ← proving value

High conviction: +$6,200 (74% win rate)
Medium:          +$3,100 (58% win rate)
Low conviction:  +$590 (45% win rate) ← consider skipping these

━━ GUARDRAILS ━━
✅ No tilt detected | 3 trades today (limit: 5)
✅ Drawdown: -2.1% from peak (limit: -15%)
✅ FOMC: May 6-7 (24 days away, no constraint)
⚠️ Earnings season Week 2 next week — IV will be elevated across
   megacap tech. Prepare for strangle opportunities.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## Operational Resilience (polish later)

These are documented for architecture completeness. Not blockers for Sprint 1.
Build them as you encounter the need.

### Overnight Assignment Detection

```python
@dataclass
class AssignmentDetector:
    """
    When you get assigned on a short put, E*Trade processes it overnight.
    Monday morning you have 100 shares that didn't exist Friday.
    
    The system must:
    1. Detect new stock positions that appeared overnight
    2. Match them to the short put that was assigned
    3. Calculate correct cost basis (strike - premium received)
    4. Auto-classify shares into Engine 1 or 2 (from onboarding conviction)
    5. Queue covered call recommendation in the morning briefing
    6. Update tax records (premium reduces cost basis, holding period starts)
    7. Update the wash sale tracker (if you had a loss on this ticker recently)
    """
    
    def detect_overnight_assignments(
        self,
        positions_yesterday: list[Position],
        positions_today: list[Position]
    ) -> list[dict]:
        """
        Compare yesterday's positions to today's.
        New stock positions that match a closed short put = assignment.
        """
        yesterday_stocks = {p.symbol: p.quantity for p in positions_yesterday 
                          if p.position_type == "long_stock"}
        today_stocks = {p.symbol: p.quantity for p in positions_today 
                       if p.position_type == "long_stock"}
        
        # Find net new shares
        assignments = []
        for symbol, qty in today_stocks.items():
            prev_qty = yesterday_stocks.get(symbol, 0)
            new_shares = qty - prev_qty
            
            if new_shares >= 100 and new_shares % 100 == 0:
                # Match to a short put that disappeared
                matching_put = self.find_closed_short_put(symbol, new_shares // 100)
                if matching_put:
                    cost_basis = matching_put.strike - matching_put.entry_price
                    assignments.append({
                        "symbol": symbol,
                        "shares": new_shares,
                        "assignment_strike": matching_put.strike,
                        "premium_received": matching_put.entry_price,
                        "cost_basis_per_share": cost_basis,
                        "account_id": matching_put.account_id,
                        "conviction": get_onboarding_conviction(symbol),
                        "engine": get_onboarding_engine(symbol),
                    })
        
        return assignments
    
    def generate_assignment_briefing(self, assignments: list[dict]) -> str:
        """Morning briefing section for overnight assignments."""
        if not assignments:
            return ""
        
        lines = ["━━ OVERNIGHT ASSIGNMENTS ━━\n"]
        for a in assignments:
            engine_label = "Engine 1 (hold, sell calls on 30-40%)" if a["engine"] == "engine1" \
                          else "Engine 2 (sell calls on 100%)"
            lines.append(
                f"📦 ASSIGNED: {a['shares']} shares {a['symbol']} @ ${a['assignment_strike']:.2f}\n"
                f"   Premium collected: ${a['premium_received']:.2f}/share\n"
                f"   Cost basis: ${a['cost_basis_per_share']:.2f}/share\n"
                f"   Routed to: {engine_label}\n"
                f"   → Sell covered calls today. System will propose strikes.\n"
            )
        return "\n".join(lines)
```

### Weekend & Holiday Schedule

```python
WEEKEND_SCHEDULE = {
    "saturday_morning": {
        "time": "09:00",
        "tasks": [
            "Run weekly learning loop (retune signal weights)",
            "Generate weekly performance review",
            "Compute weekly tax impact summary",
            "Run backtest on any new signal variations",
            "Push weekly review to Telegram",
        ]
    },
    "sunday_evening": {
        "time": "18:00", 
        "tasks": [
            "Scan next week's earnings calendar",
            "Identify options expiring Friday",
            "Check for Fed speakers / FOMC / CPI / jobs data",
            "Run Scout Agent full scan (weekend social buzz)",
            "Pre-compute opportunity scores for Monday",
            "Generate 'Week Ahead' preview briefing",
            "Push week-ahead briefing to Telegram",
        ]
    },
    "holiday_handling": {
        "half_days": "Run EOD analysis at 1:00 PM instead of 3:30 PM",
        "full_holidays": "Run Scout Agent only (social still active). No market analysis.",
        "day_before_holiday": "Close or roll any position expiring during the break",
    }
}

WEEK_AHEAD_BRIEFING = """
━━ WEEK AHEAD — April 14-18, 2026 ━━

EARNINGS THIS WEEK:
  Mon: —
  Tue: NFLX (AMC), JNJ (BMO)
  Wed: ASML (BMO), ABT (BMO)
  Thu: TSM (BMO), INTUITIVE (AMC)
  Fri: —
  
  ⚠️ TSM: you hold a short put expiring 4/18. Close or roll BEFORE Thu.

EXPIRING THIS FRIDAY (4/18):
  NVDA May-18 95P — currently 72% profit → close early Mon/Tue
  GOOG May-18 155P — currently 45% profit → monitor, close at 50%
  AMD May-18 130P — currently 28% profit → let ride, far OTM

MACRO EVENTS:
  Mon: Empire State Manufacturing
  Tue: Retail Sales (8:30 AM) — could move market
  Wed: Fed Beige Book (2:00 PM)
  Thu: Philly Fed, Existing Home Sales
  Fri: Options Expiration (monthly)

SCOUT WEEKEND BUZZ:
  CRWD: 12 mentions across 4 sources — downgrade fallout continuing
  SMCI: 8 mentions — audit resolution rumors
  
REGIME OUTLOOK: VIX at 22.1 — ATTACK mode likely continues.
Expected activity: 8-12 trades this week (earnings season ramping).
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
```

### Failure Recovery & Manual Fallback

```python
@dataclass
class FailureRecovery:
    """
    What happens when things break. The system should degrade 
    gracefully, never silently fail.
    """
    
    # === BROKER API DOWN ===
    broker_down_protocol = {
        "detection": "3 consecutive failed API calls within 5 minutes",
        "alert": "🛑 E*Trade API unreachable. Switching to manual mode.",
        "actions": [
            "Stop all automated analysis cycles",
            "Push last-known portfolio state to Telegram",
            "Send manual fallback checklist (see below)",
            "Retry connection every 5 minutes",
            "Resume automatically when connection restored",
        ],
    }
    
    # === SERVER CRASH ===
    server_crash_protocol = {
        "detection": "Railway health check fails",
        "recovery": [
            "Railway auto-restarts the container",
            "On startup: system reconciles DB state with broker state",
            "If DB is behind: re-pull all positions, re-run analysis",
            "If DB is ahead (trades logged but not confirmed): verify with broker",
            "Push recovery notification to Telegram",
        ],
    }
    
    # === DATABASE CORRUPTION ===
    database_protocol = {
        "prevention": "Daily automated Postgres backup to S3",
        "recovery": [
            "System can rebuild from broker API alone",
            "Broker is source of truth for positions and balances",
            "DB is only needed for: trade history, signal weights, tax tracking",
            "If DB lost: system still functions, just loses learning history",
            "Restore from most recent S3 backup",
        ],
    }
    
    # === MANUAL FALLBACK CHECKLIST ===
    manual_fallback = """
    IF THE SYSTEM IS DOWN, CHECK THESE 5 THINGS MANUALLY:
    
    1. Open E*Trade app. Check any positions expiring THIS WEEK.
       If any are ITM or near strike → close or roll manually.
    
    2. Check any position showing >150% of entry premium (loss warning).
       If any position is at 2x entry premium → close it manually.
    
    3. Check E*Trade alerts for any overnight assignments.
       If assigned → sell covered calls at 0.25 delta, 30 DTE.
    
    4. Check VIX on Google. If VIX >35 → close all weeklies manually.
       If VIX >40 → close everything with DTE < 14.
    
    5. Do NOT open any new positions while the system is down.
       Wait for the system to come back online.
    """


class StateReconciler:
    """
    On every startup, reconcile the system's database state 
    with the broker's actual state. Broker always wins.
    """
    
    async def reconcile(self):
        """
        Compare DB positions to broker positions.
        Fix any discrepancies. Log what changed.
        """
        db_positions = await get_db_positions()
        broker_positions = await get_broker_positions()
        
        discrepancies = []
        
        # Positions in DB but not in broker (closed/expired/assigned overnight)
        for db_pos in db_positions:
            if not find_matching_broker_position(db_pos, broker_positions):
                discrepancies.append({
                    "type": "disappeared",
                    "position": db_pos,
                    "likely_reason": infer_disappearance_reason(db_pos),
                    # Expired worthless, assigned, or closed outside the system
                })
        
        # Positions in broker but not in DB (assigned overnight, manual trades)
        for broker_pos in broker_positions:
            if not find_matching_db_position(broker_pos, db_positions):
                discrepancies.append({
                    "type": "appeared",
                    "position": broker_pos,
                    "likely_reason": "overnight_assignment" if broker_pos.quantity % 100 == 0 
                                    else "manual_trade",
                })
        
        if discrepancies:
            await alert_discrepancies(discrepancies)
            await fix_db_state(discrepancies)
```

### SIPC Coverage Check

```python
def check_sipc_coverage(accounts: dict[str, BrokerageAccount]) -> list[str]:
    """
    SIPC insures $500K per account type ($250K cash within that).
    At $1M+ across accounts, excess may be uninsured.
    
    Run during onboarding and quarterly.
    """
    alerts = []
    
    for acct_id, acct in accounts.items():
        if acct.total_value > 500_000:
            excess = acct.total_value - 500_000
            alerts.append(
                f"⚠️ {acct.account_type.upper()} account at ${acct.total_value:,.0f} "
                f"exceeds $500K SIPC coverage by ${excess:,.0f}. "
                f"This excess is uninsured if the broker fails. "
                f"Consider spreading across multiple brokers for full coverage."
            )
        
        if acct.cash_available > 250_000:
            excess_cash = acct.cash_available - 250_000
            alerts.append(
                f"⚠️ ${acct.cash_available:,.0f} cash in {acct.account_type.upper()} — "
                f"SIPC cash coverage is $250K. ${excess_cash:,.0f} excess uninsured."
            )
    
    return alerts
```

### Notification Formatting (Mobile-Optimized)

```python
class TelegramFormatter:
    """
    The morning briefing is 50+ lines. On a phone that's 7+ scrolls.
    Layer the information: summary first, details on demand.
    """
    
    def format_morning_briefing(self, briefing: str) -> list[dict]:
        """
        Split the briefing into a 3-line summary + expandable detail.
        Telegram inline keyboards support callback buttons.
        """
        
        # Message 1: 3-line summary (always visible)
        summary = {
            "text": self.extract_summary(briefing),
            "buttons": [
                ["📊 Full Briefing", "📋 Trades Only", "💰 P&L Only"],
                ["⚠️ Alerts", "🏦 Tax", "📈 Positions"]
            ]
        }
        
        # Detail sections (sent only when button tapped)
        sections = {
            "full_briefing": briefing,
            "trades_only": self.extract_section(briefing, "ATTACK PLAN"),
            "pnl_only": self.extract_section(briefing, "PORTFOLIO SCORECARD"),
            "alerts": self.extract_section(briefing, "GUARDRAILS") + 
                      self.extract_section(briefing, "TAX ALERTS"),
            "tax": self.extract_section(briefing, "TAX DASHBOARD") +
                   self.extract_section(briefing, "ACCOUNTS"),
            "positions": self.extract_section(briefing, "POSITION MANAGEMENT"),
        }
        
        return {"summary": summary, "sections": sections}
    
    def extract_summary(self, briefing: str) -> str:
        """
        3-line summary that fits on one phone screen.
        Example:
        
        🔥 ATTACK | +$380/day theta | YTD +14.2%
        3 trades proposed: NVDA, AVGO, AMD
        ⚠️ 1 tax alert, 1 earnings conflict
        """
        # Parse briefing sections and compress
        regime = self.extract_value(briefing, "REGIME")
        theta = self.extract_value(briefing, "Daily theta")
        ytd = self.extract_value(briefing, "YTD")
        num_trades = self.count_trades(briefing)
        num_alerts = self.count_alerts(briefing)
        
        return (
            f"{regime}\n"
            f"{num_trades} trades proposed | {theta}\n"
            f"{num_alerts}"
        )
```

### Dividend Auto-Routing

```python
@dataclass
class DividendRouter:
    """
    When dividends hit your account, route them based on 
    which engine the stock belongs to.
    
    At $1M with dividend payers, this is $3-8K/year that 
    should be working, not sitting in cash.
    """
    
    routing_rules = {
        "engine1": {
            "action": "reinvest",
            "method": "Buy more shares of the same stock on the next dip signal",
            "fallback": "If no dip signal within 5 trading days, buy at market",
        },
        "engine2": {
            "action": "add_to_buying_power",
            "method": "Add to Engine 2 cash pool for next put sale",
            "note": "Dividends from assigned shares fund more premium selling",
        },
    }
    
    def detect_dividend(
        self,
        cash_yesterday: float,
        cash_today: float,
        positions: list[Position],
        ex_div_calendar: dict
    ) -> list[dict]:
        """
        Detect dividend payments by comparing cash balances 
        and cross-referencing ex-div dates.
        """
        cash_increase = cash_today - cash_yesterday
        
        if cash_increase <= 0:
            return []
        
        dividends = []
        for pos in positions:
            if pos.position_type == "long_stock":
                ex_div = ex_div_calendar.get(pos.symbol)
                if ex_div and (date.today() - ex_div).days <= 5:
                    estimated_div = pos.quantity * ex_div_calendar[pos.symbol + "_amount"]
                    if abs(estimated_div - cash_increase) < 50:  # rough match
                        engine = get_onboarding_engine(pos.symbol)
                        dividends.append({
                            "symbol": pos.symbol,
                            "amount": estimated_div,
                            "shares": pos.quantity,
                            "engine": engine,
                            "routing": self.routing_rules[engine],
                        })
        
        return dividends


### Regulatory Tracking

```python
@dataclass
class RegulatoryMonitor:
    """
    Track regulatory constraints that affect trading.
    """
    
    # Pattern Day Trader (PDT)
    # Not relevant at $1M+ but track anyway for completeness
    day_trades_this_week: int = 0
    is_pdt_exempt: bool = True  # account >$25K
    
    # E*Trade house margin requirements
    # These are STRICTER than Reg-T for concentrated positions
    concentrated_position_threshold: float = 0.15  # >15% triggers higher margin
    
    def check_margin_requirements(
        self, 
        position: Position, 
        portfolio: PortfolioState,
        account: BrokerageAccount
    ) -> dict:
        """
        E*Trade margin requirements vary by position concentration
        and overall portfolio composition. This estimates the ACTUAL
        margin held, not the theoretical Reg-T minimum.
        """
        concentration = (position.market_value / portfolio.net_liquidation)
        
        if concentration > self.concentrated_position_threshold:
            # E*Trade charges higher margin for concentrated positions
            margin_rate = 0.30  # 30% instead of standard 20%
        else:
            margin_rate = 0.20  # standard Reg-T for options
        
        return {
            "position": position.symbol,
            "concentration": concentration,
            "margin_rate": margin_rate,
            "margin_required": position.market_value * margin_rate,
            "is_concentrated": concentration > self.concentrated_position_threshold,
        }
```

### ESPP/RSU Vesting Schedule Tracker

```python
@dataclass
class VestingSchedule:
    """
    Track Adobe ESPP and RSU vesting dates.
    Pre-plan the sell + redeploy action so it's ready in the 
    briefing the morning shares hit your account.
    """
    
    upcoming_vests: list[dict]  # [{date, type, estimated_shares, estimated_value}]
    
    # Standing plan (set during onboarding)
    sell_on_vest: bool = True           # sell immediately on vest date
    sell_delay_days: int = 0            # or wait N days for a better price
    
    # Redeployment split
    redeploy_engine1_pct: float = 0.40  # 40% → buy diversified core holdings
    redeploy_engine2_pct: float = 0.50  # 50% → Engine 2 buying power
    redeploy_engine3_pct: float = 0.10  # 10% → dry powder
    
    # Engine 1 targets for redeployment
    engine1_buy_targets: list[str] = None  # e.g., ["AMZN", "AVGO", "MSFT"]
    
    def get_upcoming_vests(self, days_ahead: int = 30) -> list[dict]:
        """Get vests in the next N days."""
        cutoff = date.today() + timedelta(days=days_ahead)
        return [v for v in self.upcoming_vests if v["date"] <= cutoff]
    
    def generate_vest_plan(self, vest: dict) -> str:
        """
        Pre-built action plan for a vest date.
        Appears in the morning briefing on vest day.
        """
        value = vest["estimated_value"]
        e1_amount = value * self.redeploy_engine1_pct
        e2_amount = value * self.redeploy_engine2_pct
        e3_amount = value * self.redeploy_engine3_pct
        
        targets = self.engine1_buy_targets or ["AMZN", "AVGO"]
        
        return f"""
📋 VESTING TODAY: {vest['type']} — ~{vest['estimated_shares']} ADBE shares (~${value:,.0f})

STANDING PLAN:
  1. SELL all vested shares at market open (or first dip)
  2. Redeploy proceeds:
     → ${e1_amount:,.0f} ({self.redeploy_engine1_pct:.0%}) → Engine 1: buy {', '.join(targets)} on next dip
     → ${e2_amount:,.0f} ({self.redeploy_engine2_pct:.0%}) → Engine 2: add to wheel buying power
     → ${e3_amount:,.0f} ({self.redeploy_engine3_pct:.0%}) → Engine 3: dry powder

  Tax note: ESPP shares have a complex cost basis (discount + holding period).
  If held >2 years from grant + >1 year from purchase → qualifying disposition.
  Otherwise → disqualifying disposition (ordinary income on discount portion).
  
  Current disposition type: {'QUALIFYING ✅' if vest.get('qualifying') else 'DISQUALIFYING ⚠️ (sell anyway — concentration risk outweighs tax)'}
"""
```

### Operational SQL Schema Additions

```sql
-- Overnight assignment tracking
CREATE TABLE overnight_assignments (
    id SERIAL PRIMARY KEY,
    detected_date DATE NOT NULL,
    symbol VARCHAR(10) NOT NULL,
    shares INT NOT NULL,
    assignment_strike DECIMAL(10,2),
    premium_received DECIMAL(10,4),
    cost_basis DECIMAL(10,2),
    account_id VARCHAR(50),
    engine VARCHAR(10),
    covered_call_sold BOOLEAN DEFAULT FALSE,
    covered_call_date DATE,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Dividend tracking
CREATE TABLE dividend_receipts (
    id SERIAL PRIMARY KEY,
    receipt_date DATE NOT NULL,
    symbol VARCHAR(10) NOT NULL,
    amount DECIMAL(10,2),
    shares INT,
    engine VARCHAR(10),
    routing_action VARCHAR(30),
    reinvested BOOLEAN DEFAULT FALSE,
    reinvest_date DATE,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Vesting schedule
CREATE TABLE vesting_events (
    id SERIAL PRIMARY KEY,
    vest_date DATE NOT NULL,
    vest_type VARCHAR(10) NOT NULL,     -- 'ESPP', 'RSU'
    estimated_shares INT,
    estimated_value DECIMAL(12,2),
    actual_shares INT,
    actual_value DECIMAL(12,2),
    sold BOOLEAN DEFAULT FALSE,
    sold_date DATE,
    sold_price DECIMAL(10,2),
    redeployment_plan TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

-- System health log
CREATE TABLE system_health (
    id SERIAL PRIMARY KEY,
    check_time TIMESTAMP NOT NULL,
    broker_api_status VARCHAR(10),       -- 'ok', 'degraded', 'down'
    database_status VARCHAR(10),
    last_successful_analysis TIMESTAMP,
    discrepancies_found INT DEFAULT 0,
    discrepancies_resolved INT DEFAULT 0,
    notes TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);
```

---

## Updated Sprint Plan

```
Sprint 1  (Weekend 1):  Data Pipeline — E*Trade API + market data
Sprint 2  (Weekend 2):  Analysis Engine — signals, smart strikes, sizing
Sprint 3  (Weekend 3):  Claude Briefing + Telegram delivery + Onboarding Module
Sprint 4  (Weekend 4):  DAY 1 ONBOARDING — classify portfolio, gap analysis, transition plan
                         Paper Trading Framework — simulated execution + dashboard
Sprint 5  (Weeks 5-12): PAPER TRADING PERIOD — 8 weeks, 60+ trades, validate
Sprint 6  (Weekend 13): Backtesting Framework — validate signals against 3yr history
Sprint 7  (Weekend 14): Strategy Upgrades — weeklies, strangles, spreads
Sprint 8  (Weekend 15): Advisor Layer — allocation, tax engine, account routing, liquidity
Sprint 9  (Weekend 16): Loss Management + Drawdown Decomposition
Sprint 10 (Weekend 17): Scout Agent — Reddit + News + Benzinga
Sprint 11 (Weekend 18): Scout Agent — Twitter + Discord + YouTube
Sprint 12 (Weekend 19): Continuous Monitor (5x daily analysis cycles) + Live-Price Gate
Sprint 13 (Weekend 20): Correlation Hedging + Learning Loop
Sprint 14 (Weekend 21): GO LIVE — real money, manual approval on every trade
Sprint 15 (Weekend 22): Performance Attribution + auto-execution graduation
Sprint 16 (Weekend 23): Operational polish — assignment detection, failure recovery,
                         notification formatting, dividend routing, SIPC check
Sprint 17 (Weekend 24): Vesting schedule integration, weekend/holiday schedule,
                         regulatory tracking, state reconciliation, manual fallback
```
```

---

## Community-Sourced Architecture Improvements

Patterns harvested from open-source trading bot repos and build logs.
Each one addresses a specific gap or upgrades an existing component.

### 1. Walk-Forward Backtesting (replaces standard backtest)

Source: zostaff's HMM bot, TonyMa1/walk-forward-backtester, QuantConnect docs.

The current backtester runs signals against ALL 3 years of data and optimizes 
parameters against the full dataset. This overfits — a signal might look great
because it captured one specific event. Walk-forward splits the data:

```python
@dataclass
class WalkForwardConfig:
    """
    Walk-forward optimization: train on N days, test on M days,
    step forward M days, repeat. If a signal only works in-sample
    and fails out-of-sample, it's curve-fitted garbage.
    """
    train_window: int = 252    # 1 year of trading days
    test_window: int = 126     # 6 months out-of-sample test
    step_size: int = 63        # step forward 3 months at a time
    min_trades_per_window: int = 10  # need enough trades to be meaningful


class WalkForwardBacktester:
    """
    For each walk-forward window:
    1. Optimize signal thresholds on TRAIN data
    2. Run strategy with those thresholds on TEST data (unseen)
    3. Record out-of-sample performance
    4. Step forward and repeat
    
    The FINAL result is ONLY the out-of-sample performance concatenated
    across all test windows. This is what you'd actually experience live.
    
    If in-sample Sharpe is 2.5 but out-of-sample is 0.8, the signal is
    overfitted. If both are ~1.8, the signal is robust.
    """
    
    def run(self, data, strategy, config: WalkForwardConfig) -> WalkForwardResult:
        results = []
        
        for train_start in range(0, len(data) - config.train_window - config.test_window, 
                                  config.step_size):
            train_end = train_start + config.train_window
            test_end = train_end + config.test_window
            
            train_data = data[train_start:train_end]
            test_data = data[train_end:test_end]
            
            # Optimize on train data
            best_params = optimize_parameters(strategy, train_data)
            
            # Test on UNSEEN data with those params
            oos_result = run_strategy(strategy, test_data, best_params)
            
            results.append({
                "window": f"{train_start}-{test_end}",
                "in_sample_sharpe": best_params["sharpe"],
                "out_of_sample_sharpe": oos_result["sharpe"],
                "out_of_sample_return": oos_result["return"],
                "out_of_sample_win_rate": oos_result["win_rate"],
                "overfitting_ratio": best_params["sharpe"] / max(oos_result["sharpe"], 0.01),
                # Ratio >2.0 = likely overfitted
            })
        
        return WalkForwardResult(
            windows=results,
            avg_oos_sharpe=np.mean([r["out_of_sample_sharpe"] for r in results]),
            avg_overfitting_ratio=np.mean([r["overfitting_ratio"] for r in results]),
            is_robust=all(r["out_of_sample_sharpe"] > 0.8 for r in results),
        )
```

**Integration:** Replace the backtester in `src/backtest/engine.py`. Use `walk-forward-backtester` 
library (pip installable) or implement the ~50 lines above. Every signal gets a walk-forward 
result. Signals with avg out-of-sample Sharpe < 1.0 get killed.

### 2. Multi-Agent Trade Review

Source: Byte-Ventures/claude-trader (3 reviewers + judge pattern), DEV.TO build log.

Before executing a HIGH conviction trade (>$20K capital), run it through a 3-agent 
review panel via the Claude API. Different perspectives catch different blind spots.

```python
async def multi_agent_review(trade: SizedOpportunity) -> dict:
    """
    3 Claude agents review the trade from different angles.
    A 4th agent judges the consensus.
    
    Only runs on HIGH conviction trades above $20K.
    Adds ~5 seconds and $0.03 in API costs. Worth it for large trades.
    """
    
    agents = [
        {
            "role": "Bull Advocate",
            "prompt": "You are a bullish options trader. Argue FOR this trade. "
                      "What's the best-case scenario? Why will this work?",
        },
        {
            "role": "Risk Manager",
            "prompt": "You are a conservative risk manager. Argue AGAINST this trade. "
                      "What could go wrong? What's the downside scenario? "
                      "What correlations or hidden risks exist?",
        },
        {
            "role": "Quant Analyst", 
            "prompt": "You are a quantitative analyst. Evaluate the NUMBERS only. "
                      "Is the yield adequate for the risk? Is the IV rich enough? "
                      "How does this compare to the base rate for this signal type?",
        },
    ]
    
    reviews = []
    for agent in agents:
        response = await claude_api_call(
            system=agent["prompt"],
            user=format_trade_for_review(trade),
            model="claude-sonnet-4-20250514",
            max_tokens=500
        )
        reviews.append({"role": agent["role"], "analysis": response})
    
    # Judge synthesizes
    judge_response = await claude_api_call(
        system="You are the final decision maker. Based on three analyst reviews, "
               "give a GO/NO-GO/REDUCE-SIZE recommendation with one sentence of reasoning.",
        user="\n\n".join(f"[{r['role']}]: {r['analysis']}" for r in reviews),
        model="claude-sonnet-4-20250514",
        max_tokens=200
    )
    
    return {
        "reviews": reviews,
        "verdict": judge_response,
        "should_execute": "GO" in judge_response.upper(),
        "should_reduce": "REDUCE" in judge_response.upper(),
    }
```

**Integration:** Add to `src/execution/review.py`. Call from the live-price gate validation 
for any trade with `capital_deployed > $20,000`. Include the verdict summary in the 
Telegram alert so you can see the reasoning.

### 3. Alpaca Paper Trading (replaces simulated fills)

Source: Multiple repos, Alpaca's 0DTE options backtest guide.

Current paper trading simulates fills at mid + slippage. Alpaca actually processes 
orders against a simulated order book with realistic fill logic.

```python
# Paper trading config update
paper_trading:
  engine: "alpaca"              # changed from "simulated"
  alpaca_api_key: "..."         # paper trading key (free)
  alpaca_secret_key: "..."
  alpaca_base_url: "https://paper-api.alpaca.markets"
  
  # Alpaca advantages over simulation:
  # - Realistic order book fills (not just mid + slippage estimate)
  # - Actual option chain data via Alpaca Options API
  # - Portfolio margin calculated by a real margin engine
  # - Multi-leg order support (spreads, strangles)
  # - Same API shape as live trading — no code changes to go live
  
  # For live trading, swap:
  # alpaca_base_url: "https://api.alpaca.markets"
  # (or switch to E*Trade for the actual account)
```

**Integration:** Sprint 4. Use `alpaca-py` SDK for paper trading instead of the custom 
`PaperTrader` class. Keep the `PaperTrader` as a fallback for offline testing but 
use Alpaca as the primary paper engine. The go-live switch becomes: change one URL
or swap the broker abstraction to E*Trade.

### 4. STATUS.md Pattern for Claude Code Sessions

Source: DEV.TO build log (14 sessions, 961 tool calls).

Claude Code loses context between sessions. The CLAUDE.md is static instructions.
Add a STATUS.md that gets UPDATED after every session with current state:

```markdown
# STATUS.md — Updated 2026-04-12

## Current Sprint: Sprint 2 (Analysis Engine)

## Completed
- [x] E*Trade OAuth flow (src/data/auth.py)
- [x] Portfolio position fetcher (src/data/broker.py)
- [x] yfinance market data + IV rank (src/data/market.py)
- [x] PriceHistory builder with SMAs, RSI (src/data/market.py)

## In Progress
- [ ] Signal detection (src/analysis/signals.py) — 8/13 signals implemented
  - Done: intraday_dip, multi_day_pullback, iv_rank_spike, support_bounce,
          oversold_rsi, macro_fear, skew_blowout, term_inversion
  - TODO: earnings_overreaction, sector_rotation, volume_climax, gap_fill, 
          dark_pool (deprioritized)

## Blocked
- Smart strike selection needs options chain data — waiting on E*Trade 
  sandbox key approval (submitted Apr 10, expect Apr 14)

## Known Issues  
- IV rank calculation uses realized vol as proxy — works but imprecise
  for individual names. Consider OptionsDX historical IV data ($30/mo)
- E*Trade rate limiting hit during batch quote testing — added 0.3s sleep

## Next Steps
1. Finish remaining 5 signals in signals.py
2. Implement smart strike selection (src/analysis/strikes.py)
3. Implement conviction sizing (src/analysis/sizing.py)
4. Test: run full signal scan on sample portfolio fixture

## Running Strategy
- Test threshold for RSI oversold: tried 30 and 28, 28 performs better on
  sample data. Will validate in walk-forward backtest.
```

**Integration:** Add to the CLAUDE.md instructions: "Read STATUS.md at the start of 
every session. Update STATUS.md at the end of every session." This provides instant 
context recovery and prevents re-doing completed work.

### 5. Hot-Reloadable Configuration

Source: Byte-Ventures/claude-trader (SIGUSR2 reload pattern).

Currently, changing a signal threshold requires restarting the system. 
During market hours, that means missing alerts. Hot-reload lets you change 
parameters live:

```python
import signal
import yaml

class HotReloadableConfig:
    """
    Signal thresholds, sizing parameters, and watchlist can be 
    changed without restarting the bot. Send SIGUSR2 to reload.
    
    Usage: kill -SIGUSR2 $(pgrep -f "python src/main.py")
    Or: edit trading_params.yaml and the system picks it up in <60 seconds.
    """
    
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.config = self._load()
        self.last_modified = os.path.getmtime(config_path)
        
        # Register signal handler
        signal.signal(signal.SIGUSR2, self._reload_handler)
    
    def _load(self) -> dict:
        with open(self.config_path) as f:
            return yaml.safe_load(f)
    
    def _reload_handler(self, signum, frame):
        """Reload config on SIGUSR2 signal."""
        old_config = self.config.copy()
        self.config = self._load()
        changes = self._diff(old_config, self.config)
        if changes:
            log.info(f"Config reloaded: {changes}")
            send_telegram_sync(f"⚙️ Config reloaded:\n{changes}")
    
    def check_file_changed(self):
        """Poll-based reload — check every 60 seconds."""
        current_mtime = os.path.getmtime(self.config_path)
        if current_mtime > self.last_modified:
            self._reload_handler(None, None)
            self.last_modified = current_mtime
    
    # RELOADABLE without restart:
    RELOADABLE = [
        "signals.*",             # all signal thresholds
        "sizing.*",              # conviction percentages
        "wheel.put_target_delta", # delta targets
        "wheel.min_yield*",      # yield thresholds  
        "portfolio.max_concentration*",
        "scout.source_overrides.*",
        "watchlist",             # add/remove tickers live
    ]
    
    # REQUIRES RESTART:
    RESTART_REQUIRED = [
        "broker.*",              # API credentials
        "paper_trading.engine",  # switching paper to live
        "database.*",            # Postgres connection
    ]
```

**Integration:** Wrap all config reads through `HotReloadableConfig`. The continuous 
monitor's main loop calls `config.check_file_changed()` once per minute. The learning 
loop's weekly parameter adjustments write directly to `trading_params.yaml` and the 
running system picks them up automatically — no restart, no downtime.

### 6. Dynamic Check Frequency Based on Volatility

Source: Byte-Ventures/claude-trader.

Current tripwire polling is fixed: 30s for price, 5min for IV, etc.
In a calm market, this wastes API calls. In a volatile market, 30 seconds 
might miss a flash crash. Adaptive frequency:

```python
class AdaptivePolling:
    """
    Check frequency scales with market volatility.
    Calm market = slow polling (save API calls).
    Volatile market = fast polling (catch every move).
    """
    
    def get_price_interval(self, vix: float) -> int:
        """Seconds between price checks."""
        if vix > 30:
            return 10    # crisis: check every 10 seconds
        elif vix > 25:
            return 15    # elevated: every 15 seconds
        elif vix > 20:
            return 30    # normal: every 30 seconds (current default)
        else:
            return 60    # calm: every 60 seconds (save API budget)
    
    def get_iv_interval(self, vix: float) -> int:
        """Seconds between IV recalculations."""
        if vix > 30:
            return 120   # crisis: every 2 minutes
        elif vix > 20:
            return 300   # normal: every 5 minutes (current default)
        else:
            return 600   # calm: every 10 minutes
    
    def get_full_analysis_count(self, vix: float) -> int:
        """Number of full portfolio analyses per day."""
        if vix > 30:
            return 8     # crisis: every hour
        elif vix > 20:
            return 5     # normal: 5x daily (current default)
        else:
            return 3     # calm: morning, midday, EOD only
```

**Integration:** Replace fixed intervals in the continuous monitor with 
`AdaptivePolling`. Recalculate intervals whenever VIX data refreshes.

### 7. Dual-Extreme Trade Blocking

Source: Byte-Ventures/claude-trader.

When BOTH RSI and IV are at extremes simultaneously, standard signals 
can produce false positives. Block new entries in these conditions:

```python
def check_dual_extreme_block(mkt: MarketContext, hist: PriceHistory) -> tuple[bool, str]:
    """
    When multiple indicators are at extremes simultaneously, 
    the market is in a regime that doesn't fit normal signal logic.
    Block new entries and wait for stabilization.
    """
    
    # Extreme oversold + extreme IV = potential capitulation
    # Good for Engine 1 buying, BAD for short-dated put selling
    if hist.rsi_14 < 20 and mkt.iv_rank > 85:
        return (True, 
                "🛑 DUAL EXTREME: RSI {hist.rsi_14:.0f} + IV rank {mkt.iv_rank:.0f}. "
                "Potential capitulation. Block weekly puts. "
                "Monthly puts OK if HIGH conviction. Engine 1 buying excellent.")
    
    # Extreme overbought + collapsing IV = complacency top
    if hist.rsi_14 > 80 and mkt.iv_rank < 15:
        return (True,
                "⚠️ DUAL EXTREME: RSI {hist.rsi_14:.0f} + IV rank {mkt.iv_rank:.0f}. "
                "Complacency. Premiums too thin. Wait for mean reversion.")
    
    return (False, None)
```

**Integration:** Add as an additional gate check in the opportunity scorer.
Runs after signal detection but before sizing. Prevents selling weekly puts 
into a capitulation event where gamma risk is highest.

### 8. Self-Improving Skill Generation

Source: tradermonty/claude-trading-skills (automated skill pipeline).

The learning loop tunes existing parameters. This goes further — it generates 
entirely NEW signals and strategies by mining trade logs for patterns:

```python
class SkillMiner:
    """
    Weekly: analyze closed trades for patterns the current signals DON'T capture.
    
    Example discoveries:
    - "Trades entered on Tuesdays have 12% higher win rate than Fridays"
      → New signal: day-of-week bias
    - "Trades on names with >2000 open interest at the strike have 8% better fills"
      → New filter: open interest minimum
    - "Earnings crush works 80% of the time on FAANG but only 45% on mid-caps"
      → Strategy refinement: restrict earnings crush to mega-cap only
    
    These discoveries get proposed as new parameters, reviewed, 
    and A/B tested during the next paper trading window.
    """
    
    def mine_patterns(self, trades: list[TradeRecord]) -> list[dict]:
        # Group by day of week, time of entry, market cap bucket,
        # sector, IV rank bucket, DTE bucket, and signal combination
        # Look for win rate anomalies >10% from baseline
        pass
    
    def propose_new_signal(self, pattern: dict) -> str:
        # Use Claude to draft a new signal based on the discovered pattern
        # Returns a proposed addition to signals.py
        pass
```

**Integration:** Add to the weekly learning loop. Not for live trading immediately — 
discovered patterns go into a "candidate signals" queue that gets validated by 
walk-forward backtest before being promoted to production.

### 9. Benchmark Tracking (SPY comparison)

Source: Multiple repos track benchmark comparison. Our spec was missing it.

```python
@dataclass
class BenchmarkComparison:
    """
    Track system performance vs passive benchmarks.
    If after 6 months the system isn't beating SPY wheel, you'd want to know.
    """
    
    benchmarks: dict = None  # computed
    
    def compute(self, period_start: date, period_end: date, portfolio_return: float):
        self.benchmarks = {
            "spy_buy_hold": get_spy_return(period_start, period_end),
            "qqq_buy_hold": get_qqq_return(period_start, period_end),
            "spy_vanilla_wheel": simulate_vanilla_wheel("SPY", period_start, period_end),
            # Vanilla wheel = sell 30-DTE 0.25 delta SPY puts, no signals, no sizing
            "risk_free": get_tbill_rate(period_start, period_end) * days_between / 365,
        }
        
        self.benchmarks["system_alpha"] = portfolio_return - self.benchmarks["spy_buy_hold"]
        self.benchmarks["system_vs_vanilla_wheel"] = portfolio_return - self.benchmarks["spy_vanilla_wheel"]
    
    def format_for_briefing(self) -> str:
        b = self.benchmarks
        return f"""
━━ BENCHMARK COMPARISON (YTD) ━━
Your portfolio:      {portfolio_return:+.1%}
SPY buy & hold:      {b['spy_buy_hold']:+.1%}
QQQ buy & hold:      {b['qqq_buy_hold']:+.1%}
Vanilla SPY wheel:   {b['spy_vanilla_wheel']:+.1%}
Risk-free (T-bills):  {b['risk_free']:+.1%}

Alpha vs SPY:        {b['system_alpha']:+.1%} {'✅' if b['system_alpha'] > 0 else '❌'}
Alpha vs vanilla:    {b['system_vs_vanilla_wheel']:+.1%} {'✅' if b['system_vs_vanilla_wheel'] > 0 else '❌'}
"""
```

**Integration:** Add to the weekly performance review and monthly briefings.
If system_vs_vanilla_wheel is negative for 3 consecutive months, the learning 
loop should flag "the complexity isn't paying for itself" and recommend simplifying.

### Updated Project Structure (additions highlighted)

```
wheel-copilot/
├── CLAUDE.md
├── STATUS.md                    # NEW: live state for Claude Code sessions
├── PLAN.md
├── config/
│   ├── watchlist.yaml           # hot-reloadable
│   ├── trading_params.yaml      # hot-reloadable
│   ├── greeks_targets.yaml
│   ├── scout_sources.yaml
│   └── secrets.env
├── src/
│   ├── config/
│   │   └── hot_reload.py        # NEW: hot-reloadable config manager
│   ├── data/
│   │   ├── broker.py            # broker abstraction (E*Trade + Alpaca)
│   │   ├── broker_etrade.py     # E*Trade implementation
│   │   ├── broker_alpaca.py     # NEW: Alpaca implementation (paper trading)
│   │   ├── auth.py
│   │   ├── market.py
│   │   ├── events.py
│   │   └── models.py
│   ├── analysis/
│   │   ├── signals.py
│   │   ├── strikes.py
│   │   ├── sizing.py
│   │   ├── scanner.py
│   │   ├── opportunities.py
│   │   ├── risk.py
│   │   ├── strategies.py
│   │   ├── correlation.py
│   │   ├── greeks_guard.py
│   │   ├── loss_mgmt.py
│   │   ├── drawdown.py
│   │   └── dual_extreme.py      # NEW: dual-extreme trade blocking
│   ├── backtest/
│   │   ├── engine.py
│   │   ├── walk_forward.py      # NEW: walk-forward optimization
│   │   ├── data_loader.py
│   │   ├── option_pricer.py
│   │   ├── signal_backtest.py
│   │   ├── strategy_backtest.py
│   │   ├── benchmark.py         # NEW: SPY/QQQ/vanilla wheel comparison
│   │   └── reports.py
│   ├── learning/
│   │   ├── loop.py
│   │   ├── retuner.py
│   │   ├── skill_miner.py       # NEW: discover new signals from trade data
│   │   └── audit.py
│   ├── execution/
│   │   ├── repricer.py
│   │   ├── conditional.py
│   │   ├── timing.py
│   │   ├── quality.py
│   │   └── review.py            # NEW: multi-agent trade review for large trades
│   ├── monitor/
│   │   ├── continuous.py
│   │   └── adaptive.py          # NEW: dynamic polling based on VIX
│   ├── scout/
│   │   └── ... (unchanged)
│   ├── reasoning/
│   │   └── briefing.py
│   ├── delivery/
│   │   ├── telegram_bot.py
│   │   └── trade_log.py
│   └── main.py
├── sql/
│   └── schema.sql
├── tests/
│   ├── test_walk_forward.py     # NEW
│   ├── test_multi_agent.py      # NEW
│   └── ...
└── requirements.txt
```

---

## Notes

- **Constraint solver mapping**: the three-engine allocation with regime-dependent 
  targets, conviction-based sizing, concentration limits, tax constraints, and 
  correlation caps is a natural MiniZinc problem. Engine allocation is an 
  optimization with soft constraints and priorities — TyCo for money.

- **Scout Agent** is a continuously running social intelligence pipeline that 
  feeds the morning briefing and pushes intraday alerts for time-sensitive 
  opportunities. It's the deal flow engine.

- **Execution** — E*Trade's `place_equity_order` via pyetrade with Telegram 
  approval buttons. Start human-in-the-loop, graduate to auto after 3 months 
  of tracking system vs. actual decisions.

- **40%+ target** requires all engines firing: Engine 1 appreciation during 
  bull runs + Engine 2 premium income in all conditions + opportunistic dry 
  powder deployment during crashes. The blended return compounds because 
  Engine 2 income funds Engine 1 share purchases.

- **The single most important feature** is performance attribution. Without it, 
  you can't tell whether high-conviction trades actually outperform, whether 
  Scout picks add alpha, or whether weekly puts justify the gamma risk. Build 
  it early, review it weekly, and be willing to kill strategies that don't work.
