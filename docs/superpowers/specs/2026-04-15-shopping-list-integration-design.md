# Shopping List Integration Design

## Goal

Integrate an external Google Sheets stock rating list (240 stocks with ratings and price targets, maintained by Parkev) into the Wheel Copilot as the primary discovery universe. The shopping list replaces Finviz for scanner discovery, modifies trade conviction, and powers a new BENCH section showing names approaching entry readiness.

## Architecture

Three new components feed into the existing pipeline:

1. **Data layer** (`src/data/shopping_list.py`) — fetch, cache, resolve, and serve the shopping list
2. **Conviction modifier** — rating-based adjustment applied after TradingView adjustment in `build_recommendations`
3. **Bench builder** — lightweight technical screening of top shopping list names for the briefing

The existing scanner pipeline (`scan_wheel_candidates` in `src/main.py`) is refactored to accept the shopping list as its primary input, with Finviz as a fallback.

```
Google Sheet CSV
       │
       ▼
 shopping_list.py ──► ShoppingListEntry[]
       │                     │
       ├──► Scanner (top 40 by composite score)
       │         │
       │         ▼ existing Phase 2: IV/RSI/chain/earnings gate
       │         │
       │         ▼ ScannerPick[] (top 8, with rating + target)
       │
       ├──► Conviction modifier (in build_recommendations)
       │         rating_tier adjusts conviction ±1-2 levels
       │         "Parkev" label when conviction changed
       │
       └──► Bench builder (top 30, lightweight technicals)
                 │
                 ▼ Bench display (top 10-15 in briefing)
                   compact default, detailed when near-actionable
```

## Component 1: Data Layer

### File: `src/data/shopping_list.py`

### Model

```python
@dataclass
class ShoppingListEntry:
    name: str                                      # "Alphabet"
    ticker: str                                    # "GOOG" (auto-resolved)
    rating: str                                    # "Buy", "Top 15 Stock", "Hold/ Market Perform", "Sell"
    rating_tier: int                               # 5=Top Stock, 4=Top 15, 3=Buy, 2=Borderline Buy, 1=Hold, 0=Sell
    date_updated: date | None                      # last review date from the sheet
    price_target_2026: tuple[Decimal, Decimal] | None  # (low, high) e.g. (Decimal("500"), Decimal("550"))
    price_target_2027: tuple[Decimal, Decimal] | None
    stale: bool                                    # True if date_updated > 90 days ago
```

The model lives in `src/models/shopping_list.py` per project convention (all dataclasses in `src/models/`).

### Public Exports

- `src/models/shopping_list.py` exports via `src/models/__init__.py`: `ShoppingListEntry`, `BenchEntry`
- `src/data/shopping_list.py` exports via `src/data/__init__.py`: `fetch_shopping_list`, `resolve_ticker`

### Expected CSV Column Layout

The Google Sheet exports as CSV with this header row:
```
"Name","Rating","Date Updated","2026 Price Target","As of Date","2027 Price Target","","2028 Price Target","","2029 Price Target","","2030 Price Target","","","*For informational purposes only..."
```

We parse columns 0 (Name), 1 (Rating), 2 (Date Updated), 3 (2026 Price Target), 5 (2027 Price Target). Other columns are ignored.

### Fetch Logic

`async def fetch_shopping_list(force_refresh: bool = False) -> list[ShoppingListEntry]`

Async because it performs HTTP I/O (project convention: async everywhere for I/O). Uses `httpx` for the HTTP GET and `asyncio.to_thread` for file I/O if needed.

- **Source URL:** Configured in `config/trading_params.yaml` under `shopping_list.url` (not hardcoded). Default: `https://docs.google.com/spreadsheets/d/12Fs_d8Zr4sKnoCxb5EaEbe2FciXIGPVTFGM9iehZq3M/gviz/tq?tqx=out:csv`
- **Cache file:** `config/.shopping_list_cache.csv`
- **Timestamp file:** `config/.shopping_list_fetched` (contains ISO timestamp of last successful fetch)
- **Cache TTL:** 24 hours
- **Logic:**
  1. If cache exists and is <24h old and `force_refresh` is False: read from cache
  2. Otherwise: HTTP GET the CSV URL, write to cache, update timestamp
  3. If fetch fails (network error, 4xx/5xx): fall back to stale cache with a warning log. If cache is older than 7 days, escalate to a Telegram alert (not just a log warning) — stale ratings on a live trading system are a real risk.
  4. Parse CSV rows into `ShoppingListEntry` objects
- **Rating tier mapping:**
  - "Top Stock to Buy" → 5
  - "Top 15 Stock" → 4
  - "Buy" → 3
  - "Borderline Buy" → 2
  - "Hold/ Market Perform" → 1
  - "Sell" → 0
- **Price target parsing:** Handles formats like "500-550", "1,150-1,250", "2-2.2 billion market cap" (skip non-numeric). Extracts low/high as `Decimal`. If only one number, both low and high are the same.
- **Stale detection:** `date_updated` older than 90 days from today → `stale = True`

### Name→Ticker Resolution

`resolve_ticker(company_name: str) -> str | None`

- **Manual overrides** (checked first): a dict of ~20 known problem names:
  ```python
  _MANUAL_OVERRIDES = {
      "Alphabet": "GOOG",
      "Meta Platforms": "META",
      "Taiwan Semi": "TSM",
      "British American Tob.": "BTI",
      "Eli Lilly": "LLY",
      "Pinduoduo": "PDD",
      "Mercadolibre": "MELI",
      "Booking Holdings": "BKNG",
      "The Trade Desk": "TTD",
      "Deer": "DE",
      "Carnival Cruise Line": "CCL",
      "Royal Caribbean Cruise": "RCL",
      "Procter & Gamble": "PG",
      "Keurig Dr Pepper": "KDP",
      "Unite Parsel Service": "UPS",
      "Lumen Tech": "LUMN",
      "Luminar Tech": "LAZR",
      "S&P Global": "SPGI",
      "Corning ": "GLW",
      "Exxon ": "XOM",
      "Cameco ": "CCJ",
      "PACCAR ": "PCAR",
      # Add more as resolution failures surface
  }
  ```
- **Auto-resolve:** `yf.Ticker(name).info` or `yf.Search(name)` — extract ticker from the first result
- **Cache:** Resolved mappings stored in `config/.ticker_map.json` (persistent across runs). Only re-resolve if a name is not in the cache.
- **Failure handling:** If resolution fails after auto-resolve, log a warning and skip the entry. The ticker map can be manually edited to fix stubborn cases.

## Component 2: Conviction Modifier

### Where: `src/main.py` in `build_recommendations`, after TV adjustment

### Logic

```python
def _apply_shopping_list_adjustment(
    sized: SizedOpportunity,
    shopping_list: dict[str, ShoppingListEntry],  # keyed by ticker
) -> tuple[SizedOpportunity, str | None]:
    """Adjust conviction based on shopping list rating.
    
    Returns the (possibly modified) opportunity and a label string
    if the conviction was changed, or None if no change.
    """
```

**Adjustment rules:**

| Rating Tier | Effect | Label |
|---|---|---|
| 5 (Top Stock to Buy) | +2 levels (LOW→HIGH, MEDIUM→HIGH) | "⬆ Upgraded (Top Stock — Parkev)" |
| 4 (Top 15 Stock) | +1 level (LOW→MEDIUM, MEDIUM→HIGH) | "⬆ Upgraded (Top 15 Stock — Parkev)" |
| 3 (Buy) | No change | None |
| 2 (Borderline Buy) | No change | None |
| 1 (Hold/Market Perform) | -1 level (HIGH→MEDIUM, MEDIUM→LOW) | "⬇ Downgraded (Hold — Parkev)" |
| 0 (Sell) | -1 level + warning | "⚠ Sell-rated (Parkev)" |
| Not in list | No change | None |

**Stale guard:** If `entry.stale` is True (>90 days since last update), the adjustment is neutralized — no conviction change applied. Rationale: a rating that hasn't been reviewed in 3+ months may no longer reflect the analyst's current view. The briefing still displays the rating but appends "(stale)" and does not modify conviction.

Examples:
- Stale Top Stock to Buy: no adjustment (instead of +2)
- Stale Top 15 Stock: no adjustment (instead of +1)
- Stale Hold: no adjustment (instead of -1)

**Upside calculation:**
```python
def _upside_pct(entry: ShoppingListEntry, current_price: Decimal) -> float | None:
    if entry.price_target_2026 and current_price > 0:
        midpoint = (entry.price_target_2026[0] + entry.price_target_2026[1]) / 2
        return float((midpoint - current_price) / current_price)
    return None
```

The result is a dimensionless ratio (not a dollar amount), so `float` is appropriate for the return value. Input prices are `Decimal`.

Upside is informational — displayed in the briefing but does NOT modify conviction.

### Briefing Labels

The Parkev label is stored on the `SizedOpportunity` (new optional field: `conviction_label: str | None`). The briefing formatter renders it after the trade description:

```
>>> MSFT — Sell Cash-Secured Put  ⬆ Upgraded (Top 15 Stock — Parkev)
    2x @ $12.50 mid | 38% ann. | delta 0.22 | TV BUY
```

Labels appear in DO NOW (high conviction), CONSIDER (medium/low conviction), and SCANNER PICKS.

## Component 3: Scanner Integration

### Where: `src/main.py` in `scan_wheel_candidates`

### Changes

The function signature gains a new parameter:

```python
def scan_wheel_candidates(
    watchlist: set[str],
    etrade_session=None,
    shopping_list: list[ShoppingListEntry] | None = None,
) -> list[ScannerPick]:
```

### Logic

1. **If shopping list is provided (primary path):**
   - Filter out: tickers already in watchlist, tickers rated "Sell", entries with no resolved ticker
   - For entries with a current price available (fetched in batch via yfinance), calculate upside_pct
   - Rank by composite score: `rating_tier * 3 + upside_normalized * 2 + freshness_bonus`
     - `freshness_bonus`: 1 if updated within 30 days, 0.5 if within 60 days, 0 otherwise
     - `upside_normalized`: `min(max(upside_pct, 0.0), 1.0)` — linearly maps 0-100% upside to 0.0-1.0, floors negative upside (stock above target) at 0.0, caps at 1.0. Entries without a price target get 0.0 (no upside bonus).
   - Take **top 40** by composite score
   - Run through existing Phase 2 screening (IV rank gate, RSI gate, options chain fetch, earnings gate, delta targeting)
   - If **fewer than 3** picks survive from the shopping list, **supplement** (not replace) with Finviz results (existing `discover_scanner_universe` path) up to a total of 8 picks. Shopping list picks retain their `shopping_list_rating` field; Finviz-sourced picks have `shopping_list_rating = None`.

2. **If shopping list is None (fallback):**
   - Current Finviz behavior, unchanged

### ScannerPick Extension

Add two optional fields to `ScannerPick`:

```python
@dataclass
class ScannerPick:
    # ... existing fields ...
    shopping_list_rating: str | None = None    # e.g. "Top 15 Stock"
    price_target: str | None = None            # e.g. "$500-550"
```

### Scanner Output in Briefing

```
🔍 SCANNER PICKS — from your shopping list
📝 SELL PUT: LLY [Buy] @ $710
   IV rank 65 — premium rich | RSI 32 — oversold | Target: $1,150-1,250 (+68% upside)
   Strike: $670 (6% OTM) | Exp: May 15 (30d) | Bid: $8.50 | Delta: 0.22
   Size: 1x $670 puts ($67,000 collateral, $850 premium, ~7.1% NLV)
```

When all picks came from the shopping list, the header reads "from your shopping list". If Finviz backfill was used, it reads "from your shopping list + scanner".

## Component 4: Bench Section

### File: `src/analysis/bench.py`

Bench building involves data fetching (yfinance) and ranking (analysis), so it belongs in `src/analysis/` per module ownership rules. `main.py` orchestrates but does not contain the logic.

Exports via `src/analysis/__init__.py`: `build_bench`

```python
async def build_bench(
    shopping_list: list[ShoppingListEntry],
    watchlist: set[str],
    scanner_symbols: set[str],
) -> list[BenchEntry]:
```

- Exclude: watchlist names, scanner pick names, "Sell" rated, failed ticker resolution
- Batch-fetch lightweight data for top ~30 by rating tier via yfinance: current price, RSI(14), IV rank proxy (from 252-day HV), next earnings date
- Rank by: `rating_tier * 3 + upside_normalized * 2 + iv_rank_normalized + rsi_pullback_bonus`
  - `upside_normalized`: same formula as scanner — `min(max(upside_pct, 0.0), 1.0)`, 0.0 if no target
  - `iv_rank_normalized`: `min(iv_rank / 100.0, 1.0)` — maps 0-100 IV rank to 0.0-1.0
  - `rsi_pullback_bonus`: +2 if RSI < 30, +1 if RSI < 40, 0 otherwise
- Return top 15

### Model

```python
@dataclass
class BenchEntry:
    ticker: str
    name: str
    rating: str
    current_price: Decimal          # dollar amount → Decimal per project convention
    price_target: str | None        # "500-550" (display string)
    upside_pct: float | None        # 0.12 = 12% (dimensionless ratio, float is fine)
    iv_rank: float                  # 0-100 dimensionless
    rsi: float                      # 0-100 dimensionless
    next_earnings: date | None
    near_actionable: bool           # True if any trigger condition met
    actionable_reason: str | None   # "IV rich + oversold" etc.
```

The model lives in `src/models/shopping_list.py` alongside `ShoppingListEntry`.

### Near-Actionable Triggers

A name is flagged as near-actionable if ANY of:
- `next_earnings` is within 7 days — this is a **watch** trigger, NOT a trade trigger. The name is in the pre-earnings blackout window (per CLAUDE.md: "NEVER sell puts or calls through earnings"). The flag signals that the name will unlock for post-earnings entry soon.
- `rsi` < 35 (approaching oversold — pullback entry candidate)
- `iv_rank` > 55 (premium getting rich — wheel-friendly)

When triggered, `near_actionable = True` and `actionable_reason` describes why. Earnings triggers explicitly state the blackout:
- "Earns May 5 — IN BLACKOUT, watch for post-earnings entry"
- "RSI 28 — oversold pullback entry"
- "IV rank 72 — premium rich"

The BENCH section is informational only — it never produces trade recommendations, sizing, or conviction levels. It is a watch list, not an order list.

### Briefing Format

```
📋 BENCH — shopping list names approaching entry
  🔥 HIMS  Buy     $42  → $45-55 (+15%) | IV 72 | RSI 28 | Earns May 5
     READY: IV rich + oversold. Earns May 5 — IN BLACKOUT, watch for post-earnings entry.
  🔥 CRM   Buy     $255 → $300-320 (+22%) | IV 78 | RSI 44 | Earns May 28
     READY: IV rich. Earns May 28 — IN BLACKOUT, watch for post-earnings entry.
     ⬇ Hold-rated (Parkev) — size conservatively
  AVGO  Top 15  $192 → $400-440 (+129%) | IV 38 | RSI 55 | Earns May 29
  PINS  Top 15  $38  → $46-50 (+26%) | IV 44 | RSI 41 | Earns Apr 28
  NFLX  Top 15  $985 → target N/A | IV 20 | RSI 81 | Earns Apr 16
  ...
```

- Near-actionable names get the 🔥 prefix and an expanded "READY:" line
- All others are one-line compact
- If Parkev rating would adjust conviction, a small note appears under the READY line

### Rate Limit Budget

Bench data is lightweight (yfinance batch quotes, no options chains):
- 30 symbols in a batch quote: ~3-5 seconds
- No E*Trade API calls
- Total bench overhead: <5 seconds per cycle

## Integration Points

### `src/main.py` — `run_analysis_cycle`

```python
# After step 5b (portfolio loading), before step 6 (analyst brief):
# 5c. Load shopping list
shopping_list = await fetch_shopping_list()
shopping_list_by_ticker = {e.ticker: e for e in shopping_list}

# Modify step 7: pass shopping list to build_recommendations
recommendations = build_recommendations(
    all_signals, watchlist_data,
    portfolio=portfolio_state,
    intel_contexts=intel_contexts,
    shopping_list=shopping_list_by_ticker,  # NEW
)

# Modify step 8b: pass shopping list to scanner
scanner_picks = scan_wheel_candidates(
    watchlist_set, etrade_session=etrade_session,
    shopping_list=shopping_list,  # NEW
)

# New step 8c: build bench (in src/analysis/bench.py)
from src.analysis.bench import build_bench
bench = await build_bench(
    shopping_list,
    watchlist=watchlist_set,
    scanner_symbols={p.symbol for p in scanner_picks},
)

# Pass bench to format_local_briefing
briefing = format_local_briefing(
    ...,
    bench=bench,  # NEW
)
```

### `src/main.py` — `format_local_briefing`

New parameter: `bench: list[BenchEntry] | None = None`

The BENCH section renders after SCANNER PICKS and OPPORTUNITIES, before WATCH.

### `src/main.py` — `build_recommendations`

New parameter: `shopping_list: dict[str, ShoppingListEntry] | None = None`

After TV adjustment, apply shopping list adjustment. Store `conviction_label` on the `SizedOpportunity`.

### `src/models/recommendations.py` or equivalent

Add `conviction_label: str | None = None` to `SizedOpportunity`.

## Files Created/Modified

| File | Action | Purpose |
|---|---|---|
| `src/models/shopping_list.py` | CREATE | ShoppingListEntry + BenchEntry dataclasses |
| `src/data/shopping_list.py` | CREATE | Async fetch, cache, parse, resolve tickers |
| `src/analysis/bench.py` | CREATE | Bench builder — ranking + near-actionable detection |
| `src/main.py` | MODIFY | Scanner integration, conviction modifier, briefing sections |
| `config/trading_params.yaml` | MODIFY | Add `shopping_list.url` config entry |
| `config/.shopping_list_cache.csv` | AUTO-GENERATED | Local cache of Google Sheet |
| `config/.shopping_list_fetched` | AUTO-GENERATED | Timestamp of last fetch |
| `config/.ticker_map.json` | AUTO-GENERATED | Cached name→ticker resolutions |
| `tests/test_shopping_list.py` | CREATE | Tests for fetch, parse, resolve, conviction modifier, bench |

## Testing

- **Unit tests for data layer:** parse CSV with known content, rating tier mapping, price target parsing, stale detection, ticker resolution with manual overrides
- **Unit tests for conviction modifier:** each rating tier adjusts correctly, stale halving works, label generation is correct, passthrough for unknown tickers
- **Unit tests for bench:** ranking logic, near-actionable trigger conditions, exclusion of watchlist/scanner names
- **Integration test:** mock Google Sheet response → full pipeline → verify scanner picks include shopping list data and bench section renders
- **All dollar amounts as Decimal** per project convention

## Success Criteria

- `fetch_shopping_list()` returns 200+ entries with resolved tickers
- Scanner picks primarily come from the shopping list (Finviz is fallback only)
- Conviction adjustments match the tier table exactly
- Parkev labels appear in DO NOW/CONSIDER/SCANNER when conviction was changed
- BENCH section shows 10-15 ranked names with compact/detailed formatting
- Near-actionable names (earnings <7d, RSI <35, IV >55) get expanded treatment
- Total added latency to analysis cycle: <90 seconds
- All existing tests still pass
