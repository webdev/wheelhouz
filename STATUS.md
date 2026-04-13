# STATUS.md — Updated 2026-04-13

## Current Sprint: Sprint 2 (Analysis Engine) — COMPLETE

## Project Status: Sprints 0-2 complete, ready for Sprint 3

## Completed
- [x] Full system spec: SPEC.md (8,700+ lines, 19 system components)
- [x] Agent instructions: CLAUDE.md (Karpathy principles, all domain rules)
- [x] Agent assignments: AGENTS.md (7 agents, parallel worktrees)
- [x] Dashboard prototype: wheel-copilot-dashboard.jsx
- [x] Financial projections: wheel-copilot-projection.jsx
- [x] Shareable overview: wheel-copilot-overview.html
- [x] `src/models/` — 10 shared model files, 38 exported symbols, Decimal for money
- [x] `src/config/loader.py` — hot-reloadable config manager
- [x] `config/trading_params.yaml` — signal thresholds, sizing, regime rules
- [x] `config/accounts.yaml` — account types, levels, routing rules
- [x] `config/watchlist.yaml` — 15 tickers to monitor
- [x] `src/db/schema.sql` — Postgres schema (16 tables, all money as DECIMAL)
- [x] `tests/fixtures/` — sample portfolio, market data, options chains, trades
- [x] Sprint 1: Data Pipeline (`src/data/`) — auth, broker, market, events
- [x] Sprint 2: Analysis Engine (`src/analysis/`) — signals, strikes, sizing, scanner, opportunities

## Sprint 1 Details (Data Pipeline)
- `src/data/auth.py` — E*Trade OAuth 1.0a, token persistence, interactive auth flow
- `src/data/broker.py` — fetch_accounts, fetch_portfolio, fetch_quotes, fetch_option_chain
- `src/data/market.py` — IV rank (252-day), RSI, SMAs, VIX, term structure via yfinance
- `src/data/events.py` — earnings calendar, Fed meetings, ex-div dates
- E*Trade sandbox authenticated and verified working

## Sprint 2 Details (Analysis Engine)
- `src/analysis/signals.py` — 13 alpha signal detectors + detect_all_signals aggregator
- `src/analysis/strikes.py` — smart strike selection at technical levels
- `src/analysis/sizing.py` — conviction-based sizing (HIGH/MEDIUM/LOW)
- `src/analysis/scanner.py` — existing position scanner (6 actions)
- `src/analysis/opportunities.py` — full pipeline: signals -> strikes -> sizing -> ranking
- 33 tests passing, mypy clean on 34 source files

## Not Started
- [ ] Agent 3: Risk & Tax (`src/risk/`)
- [ ] Agent 4: Execution (`src/execution/`)
- [ ] Agent 5: Scout & Monitor (`src/scout/`, `src/monitor/`)
- [ ] Agent 6: Learning & Backtest (`src/backtest/`, `src/learning/`)
- [ ] Agent 7: Delivery & UI (`src/delivery/`)
- [ ] `src/main.py` — orchestrator

## Blocked
- Alpaca paper trading account: sign up at alpaca.markets (free)
- Telegram bot token: create via @BotFather
- Railway deployment: set up at railway.app ($5-10/mo)

## Known Issues
- E*Trade sandbox balance endpoint returns errors (sandbox limitation, works in production)
- `datetime.utcnow()` deprecation warnings — switch to `datetime.now(UTC)` in future pass
- `test_data.py` requires `pyetrade` installed to collect

## Next Steps (in order)
1. Sprint 3: Briefing + Telegram + Onboarding module (`src/delivery/`)
2. Sprint 4: Onboarding + Paper Trading (Alpaca)
3. Create Alpaca paper trading account (instant, free)
4. Create Telegram bot via @BotFather (5 minutes)
