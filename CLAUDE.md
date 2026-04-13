# Wheel Copilot

## What This Is

An AI-powered options trading copilot for a $1M portfolio across 3 E*Trade accounts
(Taxable, Roth IRA, Traditional IRA). Runs a wheel strategy (covered calls + cash-secured
puts) with 13 alpha signals, 6 strategy types, 5x daily portfolio analysis, social 
intelligence, comprehensive tax optimization, a self-tuning learning loop, and a 
bloodbath protocol for market crashes.

Three-engine model: Engine 1 (45% core holdings), Engine 2 (45% active wheel), 
Engine 3 (10% dry powder). Target: 25-40% annualized depending on market conditions.

## Architecture: Multi-Agent Build

This project is designed for parallel development by 7 Claude Code agents.
Each agent owns one or more MODULES. Modules communicate through SHARED MODELS 
defined in `src/models/`. No module directly imports from another module's internal
files — they only import from `src/models/` and each module's public interface 
(`__init__.py` exports).

```
AGENT 1: Data Pipeline     → src/data/         (broker, market, events, onboarding)
AGENT 2: Analysis Engine   → src/analysis/      (signals, strikes, sizing, scanner, strategies)
AGENT 3: Risk & Tax        → src/risk/          (greeks, correlation, tax, accounts, loss, bloodbath)
AGENT 4: Execution         → src/execution/     (live-price gate, orders, paper trading, review)
AGENT 5: Scout & Monitor   → src/scout/, src/monitor/ (social intel, 5x analysis, regime, sentinel)
AGENT 6: Learning & Backtest → src/backtest/, src/learning/ (walk-forward, self-tuning, benchmarks)
AGENT 7: Delivery & UI     → src/delivery/      (telegram, briefing, formatter, onboarding flow)
ORCHESTRATOR: main.py      → wires everything together
```

---

## Karpathy Principles (ALL agents, ALL code, NO exceptions)

Derived from Andrej Karpathy's observations on LLM coding pitfalls. A trading system 
with bloated abstractions, hidden assumptions, or unchecked logic WILL lose money.

### 1. Think Before Coding
**Don't assume. Don't hide confusion. Surface tradeoffs.**

- State your assumptions explicitly in comments: `# Assumption: STCG rate 37% — verify in config`
- If the spec is ambiguous, ASK. "Should wash sale window be 30 calendar days or 30 trading days?" matters.
- If multiple approaches exist, present tradeoffs before implementing.
- Push back when the spec seems wrong. That's valuable feedback.
- If something is unclear, STOP. Name what's confusing. Ask.

### 2. Simplicity First
**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what the spec asks for.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't specified.
- No error handling for impossible scenarios.
- **If you write 200 lines and it could be 50, rewrite it.**
- Signal detectors should be 30-80 lines, not 300. If complex, the logic is wrong.

### 3. Surgical Changes
**Touch only what you must. Clean up only your own mess.**

- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it in STATUS.md — don't delete it.
- Remove imports/variables/functions that YOUR changes made unused.
- **The test:** Every changed line should trace directly to the task at hand.

### 4. Goal-Driven Execution
**Define success criteria. Loop until verified.**

```
# GOOD: goal-driven
"Implement src/analysis/signals.py per SPEC.md.
 Success criteria: 
 - All 13 signals return AlphaSignal | None
 - All money as Decimal
 - pytest tests/test_signals.py passes
 Run tests and fix until green."

# BAD: vague instruction
"Build the signals module."
```

Strong success criteria let Claude loop independently. Weak criteria require constant clarification.
Every module in SPEC.md has success criteria. Extract them. Let Claude loop.

---

## Critical Code Standards (ALL agents)

### Language & Types
- Python 3.11+, type hints on EVERY function signature
- ALL dollar amounts as `Decimal` (from `decimal` module), NEVER float
- ALL dates as `datetime.date`, ALL timestamps as `datetime.datetime` (UTC internally, ET for display)
- Dataclasses for all data models (defined in `src/models/`)
- No pandas unless doing heavy data manipulation — prefer dataclasses + lists
- Async everywhere for I/O (broker API, Claude API, Telegram)
- Every public function has a docstring explaining what it does and returns
- Tests use pytest with fixtures in `tests/fixtures/`
- Log everything to `structlog` with context (symbol, account, signal, trade_id)

### File Organization
- Shared models go in `src/models/` — NEVER define a dataclass in a module file
- Each module's `__init__.py` exports its public interface
- Config is read through `src/config/loader.py` (hot-reloadable)
- Secrets come from environment variables via `.env`, NEVER hardcoded
- SQL lives in `src/db/schema.sql` — migrations via numbered files `src/db/migrations/`

---

## Domain Rules (HARD — violating these loses real money)

### Options Safety
- NEVER recommend naked calls — only covered calls on owned stock
- NEVER sell puts or calls through earnings unless strategy is explicitly "earnings_crush"
- NEVER sell weekly options (DTE < 10) on names with earnings that week
- NEVER open a position exceeding 5% of NLV per trade (3% for MEDIUM, 1.5% for LOW)
- NEVER exceed 10% NLV in any single name, 35% in any sector
- Loss stops are HARD: 2x premium for monthlies, 1.5x for weeklies — no exceptions

### Tax Rules
- ALWAYS check wash sale tracker before opening ANY position
  - Wash sale window: 30 CALENDAR days (not trading days)
  - If ticker closed at a loss within 30 days → BLOCK new trades on that ticker
  - Track every loss-close date in `wash_sale_tracker` table
- ALWAYS check LTCG approaching dates
  - Never sell stock within 90 days of LTCG threshold if gain > $5K
  - LTCG threshold: exactly 365 days from purchase
  - Tax savings: difference between 37% STCG and 20% LTCG
- ALL options income is SHORT-TERM regardless of holding period
- Assignment cost basis = strike − premium received. Holding period starts at assignment.
- Deep ITM covered calls can RESET holding period — sell OTM/slightly ITM only on LTCG-eligible shares
- Tax rates: STCG 37%, LTCG 20%, NIIT 3.8% (apply NIIT to both)
- Every trade proposal must include estimated tax impact and net-after-tax return
- Track running YTD: realized STCG, LTCG, harvested losses, quarterly estimated payments

### Account Routing
- High-frequency premium (weekly/monthly puts, earnings crush) → Roth IRA FIRST (tax-free)
- Strangles, spreads, anything requiring Level 3+ → Taxable ONLY (IRA restrictions)
- Engine 1 long-term equity purchases → Taxable (LTCG eligible)
- Tax-loss harvesting → Taxable ONLY (need realized losses in taxable)
- ALWAYS verify: target account has sufficient buying power
- ALWAYS verify: target account has required options level
- ALWAYS verify: routing to IRA doesn't push liquid ratio below 60% minimum
- IRA constraints: no margin, no short stock, typically Level 2 max

### Liquidity Constraints
- Minimum 60% of total NLV must be liquid (accessible without penalty)
- Minimum emergency reserve: 6 months × monthly expenses (set during onboarding)
- Roth IRA: contributions are liquid, EARNINGS are locked until 59½
- Traditional IRA: everything locked until 59½ (10% penalty + income tax)
- Taxable: fully liquid
- Liquidity health check runs in every portfolio analysis cycle (5x daily)
- If IRA becomes > 40% of total NLV: flag and consider routing income to taxable

### Regime Detection & Bloodbath Protocol
- Dedicated VIX/SPY monitor runs EVERY 60 SECONDS (independent of 5x analysis)
- VIX thresholds: ATTACK (<18), HOLD (18-25), DEFEND (25-35), CRISIS (>35)
- SPY drop thresholds: elevated (-2%), severe (-3%), crisis (-5%), extreme (-8%)
- CRISIS trigger: immediately close ALL weeklies, cancel pending, block new trades
- Regime shift overrides ALL other logic

Bloodbath specifics:
- Employer crisis (ADBE -20% in 5 days): override quarterly sell to IMMEDIATE
  - Tax efficiency NEVER overrides employer-correlated risk
- Sector repricing: detect narrative divergence (winners vs losers in portfolio)
  - Don't sell winners to cover losers
  - Premium on losers during repricing = 3-5x normal (best selling opportunity)
- Crisis spread handling: bid-ask > 15% of premium → aggressive fill (walk then market)
- Margin stress: "if market drops 3% more from HERE, margin call?" → preemptive close
- Crisis correlation: assume 0.95 across all tech/semi when VIX > 30
- Recovery attack: deploy after 3+ stabilization signals. Scale in over days, not hours.
  Monthly puts only (no weeklies). Keep 25-50% powder reserve.

### Sizing
- HIGH conviction: 3-5% of NLV, 3+ signals, IV rank > 60
- MEDIUM: 1.5-3% of NLV, 2 signals, IV rank > 45
- LOW: 0.5-1.5% of NLV, 1 signal, IV rank > 30
- Multi-agent Claude review required for any trade > $20K
- Dual-extreme block: RSI < 20 AND IV rank > 85 → block weekly puts

### The 13 Alpha Signals
Each returns `AlphaSignal | None`:
1. `intraday_dip`: stock -2%+ intraday, no news
2. `multi_day_pullback`: 3+ red days, -5%+ total
3. `iv_rank_spike`: IV rank > 60
4. `support_bounce`: within 1% of 50/100/200 SMA
5. `oversold_rsi`: RSI(14) < 30
6. `macro_fear`: VIX spike + broad selling
7. `skew_blowout`: put skew > 2σ above 30-day mean
8. `term_inversion`: front-month IV > back-month IV
9. `earnings_overreaction`: post-earnings gap > 8%, RSI < 25
10. `sector_rotation`: relative strength < -2σ
11. `volume_climax`: volume > 3x 20-day avg on down day
12. `gap_fill`: filling toward prior close after gap down
13. `dark_pool`: unusual dark pool activity (deprioritized)

### The 6 Strategies
1. `monthly_put`: 30-45 DTE, 0.20-0.30 delta
2. `weekly_put`: 5-10 DTE, 0.15-0.20 delta, dip-only
3. `strangle`: short put + call, 0.15 delta each, 30-45 DTE
4. `earnings_crush`: sell 1-3 DTE before earnings, IV > 80th pct
5. `put_spread`: 0.25/0.15 delta spread, defined risk
6. `dividend_capture`: sell puts before ex-div

### 5x Daily Analysis
1. 8:00 AM — Morning Briefing (always push)
2. 10:30 AM — Post-Opening Assessment (push if material change)
3. 1:00 PM — Midday Check (silent if healthy)
4. 3:30 PM — End-of-Day Assessment (push if action needed)
5. 4:30 PM — Post-Market Review (always push)

Pre-market sentinel: 6am, 7am, 7:30am (futures + VIX futures check).
Weekend: Saturday AM = learning loop. Sunday PM = week-ahead prep.

### Live-Price Gate
No fixed timers. User taps EXECUTE, system validates in 2 seconds:
1. Underlying within ±3% of analysis price
2. Premium ≥ 80% of analysis premium
3. IV rank still above minimum
4. Delta within range
5. No new disqualifying events
6. Bid-ask < 15% of premium
ALL must pass. Any failure → reject with explanation.

### Day 1 Onboarding
1. Discover all E*Trade accounts (type, balance, options level)
2. Ask Roth contribution basis (liquid vs locked)
3. Ask monthly expenses (emergency reserve)
4. Set liquidity constraints
5. Auto-classify options → E2, cash → E3
6. Each stock: user classifies E1 or E2 via Telegram
7. Gap analysis (stranded profits, earnings conflicts, concentration)
8. Phased transition plan (immediate / short-term / medium-term)
9. Never force tax-inefficient sales. Never close working positions at bad times.

### ADBE Concentration
- Track as special case. Quarterly sell plan to <15% NLV.
- Emergency: ADBE -20% in 5 days → sell immediately, override tax optimization.
- ESPP/RSU vesting: countdown, pre-plan sell + redeploy.

### Learning Loop
- Weekly (Saturday). Walk-forward backtest: train 252d, test 126d, step 63d.
- Adjust signal weights from OOS performance. Cap: 15% change per cycle.
- Signal OOS Sharpe < 0.8 → flag for review.
- Skill miner: discover patterns from closed trades (propose, don't auto-deploy).
- Benchmark: vs SPY, QQQ, vanilla SPY wheel. 3 months underperforming vanilla → simplify.

---

## E*Trade Specific
- OAuth 1.0a tokens expire midnight ET — auto-refresh 11:50 PM
- Rate limit: 4 req/s market data, 2 req/s account — enforce 0.3s sleeps
- Batch quotes: max 25 symbols per call
- IV rank NOT provided — calculate from yfinance 252-day historical vol
- Levels: 1 = CCs, 2 = + CSPs, 3 = + spreads, 4 = + strangles/naked
- IRAs: typically Level 2 max, no margin, no short stock

## Alpaca (Paper Trading)
- Free API. Base: `https://paper-api.alpaca.markets`
- Options via `alpaca-py>=0.20`
- Use Sprints 4-12, then switch to E*Trade for live

---

## Key Files
- `SPEC.md` — full system specification (read YOUR module's section)
- `src/models/` — shared data models (ALL agents read)
- `config/trading_params.yaml` — all tunable thresholds
- `config/accounts.yaml` — account structure and routing rules
- `STATUS.md` — current build progress (read at start, update at end)

## Testing
- Fixtures in `tests/fixtures/`: sample_portfolio, market_data, options_chain, trades
- Mock ALL external API calls — never hit live APIs in CI
- `pytest tests/ -v --tb=short`

## Running
```bash
python src/main.py                    # Full system
python src/main.py --mode briefing    # Single morning briefing
python src/main.py --mode paper       # Paper trading (Alpaca)
python src/main.py --mode backtest    # Run backtests
python src/main.py --mode onboard     # First-time onboarding
python src/main.py --mode weekend-review  # Saturday learning loop
```

## STATUS.md Protocol
Read at start. Update at end. Include: completed, in-progress, blocked, issues, next steps.

---

## Claude Code Workflow

### Parallel Worktrees
```bash
git worktree add ../wc-agent1-data agent1/data
git worktree add ../wc-agent2-analysis agent2/analysis
git worktree add ../wc-agent3-risk agent3/risk
git worktree add ../wc-agent4-execution agent4/execution
git worktree add ../wc-agent5-monitor agent5/monitor
git worktree add ../wc-agent6-learning agent6/learning
git worktree add ../wc-agent7-delivery agent7/delivery
```

### Plan Mode First
Start complex tasks in plan mode. SPEC.md IS the plan. When stuck, re-plan, then re-implement.

### Two-Claude Review
```
# Worktree A: build
claude "Implement src/risk/tax_engine.py per SPEC.md"

# Worktree B: review
claude "Review src/risk/tax_engine.py — check wash sales, Decimal, type hints, edge cases"
```

### Compounding CLAUDE.md
After every correction: "Update CLAUDE.md so you don't make that mistake again."

### Verification (Karpathy #4)
Give Claude success criteria and let it loop. Tests, fixtures, mypy. "Prove it works."

### Prompts That Work
```
# Goal-driven (preferred):
"Implement X. Success criteria: [list]. Run tests. Fix until green."

# Simplify:
"This is 300 lines, could be 80. Rewrite elegantly."

# Tradeoffs first:
"2-3 approaches with tradeoffs before implementing."

# Surgical check:
"Diff vs main. Every changed line must trace to the task."
```

### Custom Review Agents
```yaml
# .claude/agents/tax-reviewer.md
Check: wash sales (30 cal days), LTCG (365 days), Roth routing, Decimal, tax rates.

# .claude/agents/risk-auditor.md
Check: regime fires in 120s, margin stress conservative, crisis correlation 0.95.
```

---

## Sprint Plan
```
Sprint 1  (Wk 1):   Data Pipeline — E*Trade API + market data
Sprint 2  (Wk 2):   Analysis — signals, strikes, sizing
Sprint 3  (Wk 3):   Briefing + Telegram + Onboarding module
Sprint 4  (Wk 4):   Onboarding + Paper Trading (Alpaca)
Sprint 5  (Wk 5-12): PAPER TRADING — 8 weeks, 60+ trades
Sprint 6  (Wk 13):  Backtesting — walk-forward
Sprint 7  (Wk 14):  Strategies — weeklies, strangles, spreads
Sprint 8  (Wk 15):  Advisor — tax, accounts, routing, liquidity
Sprint 9  (Wk 16):  Loss mgmt + drawdown
Sprint 10 (Wk 17):  Scout — Reddit, news, Benzinga
Sprint 11 (Wk 18):  Scout — Twitter, Discord, YouTube
Sprint 12 (Wk 19):  Continuous Monitor + Live-Price Gate
Sprint 13 (Wk 20):  Correlation + Learning Loop
Sprint 14 (Wk 21):  GO LIVE — manual approval on everything
Sprint 15 (Wk 22):  Attribution + auto-execution graduation
Sprint 16 (Wk 23):  Operational polish
Sprint 17 (Wk 24):  Vesting, weekends, regulatory, reconciliation
```

Go-live: WR ≥ 55%, HIGH WR ≥ 65%, max DD < 12%, loss stops triggered 3+.
Auto-exec: M1-2 manual → M3 auto-close → M4 auto-HIGH → M5+ auto-HIGH+MEDIUM.
