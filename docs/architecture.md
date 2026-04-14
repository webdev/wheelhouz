# Wheel Copilot — System Architecture

An AI-powered options trading copilot for a ~$1M portfolio across 3 E*Trade accounts (Taxable, Roth IRA, Traditional IRA). Runs a wheel strategy — sell cash-secured puts on dips, sell covered calls on strength — with 17 alpha signals, Greek-aware risk management, 5x daily portfolio analysis, and a self-tuning learning loop.

---

## How It Works (30-Second Version)

Every day, the system:
1. **Scans** your watchlist for dip signals (sell puts) and strength signals (sell calls)
2. **Reviews** every open position — should you close, roll, watch, or hold?
3. **Sizes** any new trade by conviction level and concentration limits
4. **Stress-tests** rolls for 10% and 20% stock drops before recommending them
5. **Delivers** a briefing with DO NOW / CONSIDER / WATCH / SKIP sections

No trade executes without your approval. The system recommends; you decide.

---

## Signal Detection Engine — 17 Alpha Signals

Signals are the entry triggers. Each detector returns a signal with strength (0-100) and direction (sell_put or sell_call). Multiple signals on the same stock = higher conviction = larger position.

### Put Signals (Sell Puts on Weakness/Fear)

| # | Signal | Trigger | What It Means |
|---|--------|---------|---------------|
| 1 | **Intraday Dip** | Stock down 2.5%+ today | Sell into intraday fear |
| 2 | **Multi-Day Pullback** | 3+ red days, down 5%+ from 5-day high | Capitulation in progress |
| 3 | **IV Rank Spike** | IV rank > 60, jumped 20+ pts in 5 days | Premium is unusually rich |
| 4 | **Support Bounce** | Price within 3% of 200 SMA, 50 SMA, or 52w low | Institutional buying level |
| 5 | **Oversold RSI** | RSI(14) < 30 | Mean reversion candidate |
| 6 | **Macro Fear** | VIX > 25 and rising 2+ pts/day | Broad fear = fat premiums |
| 7 | **Skew Blowout** | Put skew > 1.3x its 30-day mean | OTM puts are overpriced |
| 8 | **Term Inversion** | Front-month IV > back-month IV | Market pricing near-term fear |
| 9 | **Earnings Overreaction** | Post-earnings gap > 8%, RSI < 25 | Likely oversold reaction |
| 10 | **Sector Rotation** | Stock down 5%+ in 5 days, VIX flat | Sector-specific, not systemic |
| 11 | **Volume Climax** | Volume 3x average on a down day | Exhaustion selling |
| 12 | **Gap Fill** | Filling toward prior close after gap down | Gap fill support zone |
| 13 | **Dark Pool** | (Placeholder — needs data feed) | — |

### Call Signals (Sell Covered Calls on Strength)

| # | Signal | Trigger | What It Means |
|---|--------|---------|---------------|
| 14 | **Overbought RSI** | RSI(14) > 70 | Stock extended, sell into it |
| 15 | **Resistance Test** | Price within 3% of 52w high or SMA resistance | Likely to stall here |
| 16 | **Multi-Day Rally** | 3+ green days, up 5%+ from 5-day low | Momentum exhausting |
| 17 | **Volume Climax Up** | Volume 3x average on an up day | Exhaustion buying |

**Safety rule:** Call signals only produce recommendations on stock you own (100+ shares). The system never recommends naked calls.

---

## Conviction & Position Sizing

Signals aggregate into conviction levels that drive position size:

| Conviction | Criteria | NLV Allocation |
|------------|----------|----------------|
| **HIGH** | 3+ signals, avg strength ≥ 70 | 3-5% of NLV |
| **MEDIUM** | 2+ signals, avg strength ≥ 50 | 1.5-3% of NLV |
| **LOW** | 1 signal (any strength) | 0.5-1.5% of NLV |

**A single signal NEVER qualifies above LOW.** You need convergence — multiple independent reasons to enter a trade.

### TradingView Consensus Adjustment

After sizing, TradingView's crowd consensus (26 technical indicators) adjusts conviction:
- **STRONG_SELL** → force SKIP (never trade against strong crowd consensus)
- **SELL** → cap at LOW (watch list only)
- **BUY/STRONG_BUY** → upgrade one level (crowd confirms thesis)
- **NEUTRAL** → no change

---

## Position Review System

Every open position is continuously evaluated through a priority waterfall:

### Action Priority (first match wins)

**1. CLOSE NOW** — Urgent action required
- Loss stop hit: option price rose to 2x entry (monthlies) or 1.5x (weeklies)
- Earnings before expiry with ≤ 30 DTE — close or roll before the report

**2. TAKE PROFIT** — Lock in gains and redeploy capital
- Thresholds scale by moneyness (delta):
  - Deep OTM (|delta| < 0.10): take profit at **80%** captured — let it ride, near-zero assignment risk
  - Moderate OTM (|delta| 0.10-0.25): standard **50%** threshold
  - Near ATM (|delta| > 0.25): take profit at **40%** — real risk, close early
- Time-based: ≤ 14 DTE with 30%+ captured → gamma risk rising, close and roll

**3. WATCH CLOSELY** — Something changed, here's what to do about it
- Earnings approaching (with specific guidance based on moneyness and P&L)
- TradingView consensus flipped bearish
- Confirmed downtrend on a short put
- IV rank collapsed below 30

**4. HOLD** — Thesis intact, everything healthy

---

## Greek-Aware Roll Recommendations

When the system suggests rolling a position, it doesn't just pick the nearest strike — it runs a full risk analysis:

### Strike Selection by Delta

| IV Environment | Put Target Delta | Put Max Delta | Call Target Delta |
|----------------|-----------------|---------------|-------------------|
| Normal (IV rank ≤ 60) | 0.22 | 0.30 | 0.25 |
| High (IV rank > 60) | 0.16 | 0.22 | 0.18 |

High IV → go further OTM to reduce assignment risk while still collecting rich premium.

### Stress Testing

Every put roll is stress-tested:
- **10% stock drop**: How much do you lose per contract?
- **20% stock drop**: Worst-case scenario per contract
- **Risk/Reward ratio**: If 10% drop loss > 3x premium collected → **roll blocked**

### Earnings Handling

When earnings fall within the roll window, the system targets a **post-earnings expiration** (earnings date + 30 days) instead of blocking the roll entirely. Warning is attached so you know.

### Roll Types
- **Out**: Same strike, later expiration
- **Down and Out**: Lower strike + later expiration (more conservative)
- **Up and Out**: Higher strike + later expiration (more aggressive)

---

## Risk Management Layers

### Portfolio-Level Greek Limits

| Regime | Beta-Weighted Delta (SPY equivalent) |
|--------|--------------------------------------|
| Attack (VIX < 18) | 200-500 |
| Hold (VIX 18-25) | 100-300 |
| Defend (VIX 25-35) | -100 to 150 |
| Crisis (VIX > 35) | -200 to 50 |

- **Vega cap**: 2% of NLV per 1-point IV move
- **Max portfolio beta**: 1.50
- **Theta target**: Attack = $200+/day; Hold = $100/day

### Concentration Limits
- Max 10% of NLV in any single name
- Max 35% in any single sector
- HIGH conviction: 3-5% per trade max
- LOW conviction: 0.5-1.5% per trade max

### Loss Stops (Hard — No Exceptions)
- Monthlies: close at 2x premium paid
- Weeklies: close at 1.5x premium paid

---

## Market Regime Detection

VIX and SPY are monitored continuously (every 60 seconds during market hours).

### VIX Regimes

| Regime | VIX Range | Target Deployment | Action |
|--------|-----------|-------------------|--------|
| **ATTACK** | < 18 | 90% | Sell premium aggressively |
| **HOLD** | 18-25 | 70% | Normal operations |
| **DEFEND** | 25-35 | 40% | Reduce exposure |
| **CRISIS** | > 35 | 10% | Close weeklies, block new trades |

### SPY Drop Escalation (Overrides VIX)

| SPY Drop | Severity | Override |
|----------|----------|---------|
| -2% | Elevated | Warning |
| -3% | Severe | Attack → Defend |
| -5% | Crisis | Forced to Crisis / 10% deployed |
| -8% | Extreme | Full crisis mode |

---

## Data Pipeline

| Source | What It Provides | Update Frequency |
|--------|-----------------|------------------|
| **E*Trade API** | Live option chains (real bid/ask), full Greeks (delta, gamma, theta, vega, rho, IV), portfolio positions, account balances | Per analysis cycle, rate-limited 4 req/s |
| **yfinance** | Fallback option chains, VIX/SPY macro data, historical prices for IV rank calculation | Per analysis cycle |
| **TradingView** | Technical consensus (STRONG_BUY through STRONG_SELL) from 26 indicators, oscillator and MA sub-scores | Cached 30 min |

### Analysis Schedule — 5x Daily

| Time | Cycle | Push? |
|------|-------|-------|
| 8:00 AM | Morning Briefing | Always |
| 10:30 AM | Post-Opening | If material change |
| 1:00 PM | Midday Check | If unhealthy |
| 3:30 PM | End-of-Day | If action needed |
| 4:30 PM | Post-Market Review | Always |

Pre-market sentinel: 6:00, 7:00, 7:30 AM (futures + VIX futures).

---

## Tax & Account Routing

### Account Priority
- **Roth IRA** (first): High-frequency premium income is tax-free
- **Traditional IRA** (second): Tax-deferred
- **Taxable** (last): STCG at 37% + 3.8% NIIT

### Routing Rules
- Weekly/monthly puts, earnings crush → Roth IRA first
- Strangles, spreads (Level 3+) → Taxable only (IRA restrictions)
- Long-term equity → Taxable (LTCG eligible)
- Tax-loss harvesting → Taxable only

### Tax Safety Rails
- **Wash sale tracker**: 30 calendar day window — blocks re-entry after a loss-close
- **LTCG protection**: Won't sell stock within 90 days of long-term threshold if gain > $5K
- **Deep ITM call warning**: Selling deep ITM covered calls can reset holding period
- Every trade proposal includes estimated tax impact

---

## Smart Strike Selection

Strikes are picked at technically meaningful levels, not arbitrary deltas:

**For puts (support levels below price):**
- 200 SMA support
- 50 SMA support
- 52-week low
- 20-day swing low
- 90-day VWAP
- Round numbers (5%, 10%, 15% below current)

**For calls (resistance levels above price):**
- 200 SMA resistance
- 50 SMA resistance
- 52-week high
- 20-day swing high
- Round numbers (5%, 10%, 15% above current)

Real chain data (bid/ask/delta) from E*Trade is used when available; estimated otherwise.

---

## Briefing Output Structure

```
============================================================
  WHEEL COPILOT — Tuesday, April 14, 2026
  HOLD | VIX 18.4 | SPY +1.22%
============================================================

━━ DO NOW ━━           ← Urgent: close/roll positions + HIGH conviction new trades
━━ CONSIDER ━━         ← MEDIUM and LOW conviction opportunities
━━ WATCH ━━            ← Positions with changes + earnings + tax alerts (with rolls)
━━ ANALYST BRIEF ━━    ← AI reasoning across all contexts (when available)
━━ SKIP ━━             ← Names examined and rejected, with reasoning
```

Every section answers "what should I do?" — no raw data dumps.

---

## Learning Loop (Weekly, Saturday)

- Walk-forward backtest: train 252 days, test 126 days, step 63 days
- Signal weight adjustments capped at 15% per cycle
- Signals with OOS Sharpe < 0.8 get flagged for review
- Benchmarked against: SPY buy-and-hold, QQQ, vanilla SPY wheel
- 3 months underperforming vanilla wheel → simplify the system

---

## Three-Engine Portfolio Model

| Engine | Allocation | Purpose |
|--------|-----------|---------|
| **Engine 1** | 45% | Core holdings — long stock positions |
| **Engine 2** | 45% | Active wheel — puts and covered calls |
| **Engine 3** | 10% | Dry powder — cash reserve for opportunities |

Target: 25-40% annualized depending on market conditions.
