# Agent Quick-Start Guide

Open Claude Code in the project root. Read your section, build your module.

## Setup: Parallel Worktrees (Boris Cherny's #1 tip)

Before ANY agent starts, set up worktrees so all 7 agents work in parallel:

```bash
# From the main repo root:
git worktree add ../wc-agent1-data agent1/data
git worktree add ../wc-agent2-analysis agent2/analysis
git worktree add ../wc-agent3-risk agent3/risk
git worktree add ../wc-agent4-execution agent4/execution
git worktree add ../wc-agent5-monitor agent5/monitor
git worktree add ../wc-agent6-learning agent6/learning
git worktree add ../wc-agent7-delivery agent7/delivery

# Each agent opens Claude Code in their worktree
cd ../wc-agent1-data && claude
```

## ALL AGENTS: First 5 Minutes
```bash
# 1. Read instructions + status
cat CLAUDE.md          # 3 min — rules, standards, Boris workflow patterns
cat STATUS.md          # 1 min — what's done, what's blocked

# 2. Read ONLY your sections of SPEC.md
# Section 2 (Shared Models) + your module section + Section 10 (Integration Contracts)

# 3. Start in plan mode
# "Read SPEC.md sections 2, [N], and 10. Plan the implementation of my module."
# Review the plan, then: "Execute the plan."

# 4. After implementation, verify
# "Prove to me this works. Run tests and diff against expected fixture outputs."

# 5. Before merging, get reviewed
# "Grill me on these changes. Don't make a PR until you're confident."

# 6. Update STATUS.md with what you built
```

## Agent 1: Data Pipeline
**Read:** SPEC.md Sections 2, 3, 10
**Build:** `src/data/` — broker abstraction, E*Trade OAuth, market data, events, options chains
**Test with:** `tests/fixtures/sample_portfolio.json`
**Done when:** `pytest tests/test_data.py` passes with all mocked API calls

## Agent 2: Analysis Engine
**Read:** SPEC.md Sections 2, 4, 10
**Build:** `src/analysis/` — 13 signals, smart strikes, sizing, scanner, strategies
**Depends on:** `src/models/` (shared types), Agent 1 output types (MarketContext, PriceHistory)
**Done when:** All 13 signals have unit tests, sizing respects all constraints

## Agent 3: Risk & Tax
**Read:** SPEC.md Sections 2, 5, 10
**Build:** `src/risk/` — Greeks guard, tax engine, account router, bloodbath, margin stress
**Critical:** Wash sale tracker must NEVER miss a blocking event
**Done when:** Tax engine correctly blocks wash sales, routes to Roth, detects LTCG approaching

## Agent 4: Execution
**Read:** SPEC.md Sections 2, 6, 10
**Build:** `src/execution/` — live-price gate, Alpaca paper trading, multi-agent review
**Done when:** Gate validates all conditions, Alpaca orders submit and fill correctly

## Agent 5: Scout & Monitor
**Read:** SPEC.md Sections 2, 7, 10
**Build:** `src/scout/`, `src/monitor/` — social intel, 5x analysis, regime detector, sentinel
**Critical:** Regime detector must fire within 120 seconds of VIX crossing threshold
**Done when:** Regime monitor runs independently, sentinel checks futures pre-market

## Agent 6: Learning & Backtest
**Read:** SPEC.md Sections 2, 8, 10
**Build:** `src/backtest/`, `src/learning/` — walk-forward engine, benchmarks, weekly review
**Critical:** Walk-forward must use ONLY out-of-sample results. Overfit ratio >2.0 = kill signal.
**Done when:** Walk-forward runs on sample data, learning loop proposes capped adjustments

## Agent 7: Delivery & UI
**Read:** SPEC.md Sections 2, 9, 10
**Build:** `src/delivery/` — Telegram bot, briefing generator, mobile formatter, onboarding flow
**Done when:** Morning briefing generates from fixture data, onboarding flow classifies positions

## After All Agents Complete
1. Orchestrator (any agent): wire `main.py`
2. Integration tests: end-to-end morning briefing from fixture data
3. Deploy to Railway
4. Run onboarding on live E*Trade accounts
5. Start 8-week paper trading period
