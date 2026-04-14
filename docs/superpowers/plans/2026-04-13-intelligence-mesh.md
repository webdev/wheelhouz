# Intelligence Mesh Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform the briefing from a mechanical signal printer into a multi-source intelligence synthesizer that produces analyst-quality reasoning with thesis, dissent, and transparent thinking.

**Architecture:** Three parallel workstreams (data fixes, TradingView layer, Claude reasoning engine) converge into a unified `IntelligenceContext` model per symbol. The briefing pipeline collects intelligence from all sources, then hands the context to Claude for synthesis. Position review runs before entry scan.

**Tech Stack:** Python 3.11+, tradingview-ta, yfinance (existing), anthropic (existing), dataclasses, structlog

**Spec:** `docs/superpowers/specs/2026-04-13-intelligence-mesh-design.md`

---

## File Map

### New Files
| File | Responsibility |
|------|----------------|
| `src/models/intelligence.py` | `IntelligenceContext`, `QuantIntelligence`, `TechnicalConsensus`, `OptionsIntelligence`, `PortfolioContext` dataclasses |
| `src/data/tradingview.py` | `fetch_tradingview_consensus()` with 1-hour TTL cache |
| `src/intelligence/builder.py` | `build_intelligence_context()` — assembles all sources per symbol |
| `src/intelligence/__init__.py` | Public exports |
| `src/delivery/reasoning.py` | Claude reasoning engine — prompt construction, API call, response parsing |
| `src/data/portfolio.py` | `load_portfolio_state()`, `alpaca_position_to_position()` — broker position loading |
| `src/intelligence/position_review.py` | `review_position()`, `format_position_review()` — hold/watch/close logic |
| `tests/test_intelligence.py` | Tests for models, builder, TradingView, reasoning, portfolio, position review |
| `tests/fixtures/intelligence.py` | Factory functions for `IntelligenceContext` and sub-models |

### Modified Files
| File | Change |
|------|--------|
| `src/models/market.py:106-128` | Add `OptionContract` dataclass, extend `OptionsChain` with `puts`/`calls` lists |
| `src/data/market.py:26-86` | Fix `calculate_iv_rank()` to use HV fallback when `current_iv=0` |
| `src/data/market.py:166-249` | Remove IV rank guard in `fetch_market_context()`, always call `calculate_iv_rank()` |
| `src/data/market.py` (new function) | Add `fetch_options_chain()` for real yfinance chain data |
| `src/analysis/strikes.py:63-66` | Prefer real chain data over estimated premium formula |
| `src/intelligence/__init__.py` | Add position review exports |
| `src/delivery/__init__.py` | Add reasoning engine exports |
| `src/main.py:140-370` | Rewire `run_analysis_cycle()` and `format_local_briefing()` to use intelligence mesh, position review, portfolio loading |
| `tests/fixtures/market_data.py:80-99` | Update `make_options_chain()` for new `OptionContract` fields |
| `pyproject.toml` | Add `tradingview-ta` dependency |

---

## Task 1: IntelligenceContext Data Models

**Files:**
- Create: `src/models/intelligence.py`
- Create: `tests/fixtures/intelligence.py`
- Test: `tests/test_intelligence.py`

- [ ] **Step 1: Write the failing test for model creation**

```python
# tests/test_intelligence.py
"""Tests for intelligence mesh models."""
from __future__ import annotations

from decimal import Decimal

from src.models.intelligence import (
    IntelligenceContext,
    OptionsIntelligence,
    PortfolioContext,
    QuantIntelligence,
    TechnicalConsensus,
)


class TestIntelligenceModels:
    def test_create_quant_intelligence(self) -> None:
        qi = QuantIntelligence(
            signals=[],
            signal_count=0,
            avg_strength=0.0,
            iv_rank=50.0,
            iv_percentile=50.0,
            rsi=45.0,
            price_vs_support={},
            trend_direction="range",
        )
        assert qi.iv_rank == 50.0

    def test_create_technical_consensus(self) -> None:
        tc = TechnicalConsensus(
            source="tradingview",
            overall="BUY",
            oscillators="NEUTRAL",
            moving_averages="BUY",
            buy_count=10,
            neutral_count=5,
            sell_count=3,
            raw_indicators={},
        )
        assert tc.overall == "BUY"

    def test_create_full_context(self) -> None:
        ctx = IntelligenceContext(
            symbol="NVDA",
            quant=QuantIntelligence(
                signals=[], signal_count=0, avg_strength=0.0,
                iv_rank=0.0, iv_percentile=0.0, rsi=50.0,
                price_vs_support={}, trend_direction="range",
            ),
            technical_consensus=None,
            options=None,
            portfolio=PortfolioContext(
                existing_exposure_pct=0.0,
                existing_positions=[],
                account_recommendation="Roth IRA",
                wash_sale_blocked=False,
                earnings_conflict=False,
                available_capital=Decimal("500000"),
            ),
            market=None,
            events=None,
        )
        assert ctx.symbol == "NVDA"
        assert ctx.technical_consensus is None

    def test_missing_sources_are_none(self) -> None:
        ctx = IntelligenceContext(
            symbol="AAPL",
            quant=QuantIntelligence(
                signals=[], signal_count=0, avg_strength=0.0,
                iv_rank=0.0, iv_percentile=0.0, rsi=50.0,
                price_vs_support={}, trend_direction="range",
            ),
            technical_consensus=None,
            options=None,
            portfolio=PortfolioContext(
                existing_exposure_pct=0.0,
                existing_positions=[],
                account_recommendation="",
                wash_sale_blocked=False,
                earnings_conflict=False,
                available_capital=Decimal("0"),
            ),
            market=None,
            events=None,
        )
        assert ctx.options is None
        assert ctx.market is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_intelligence.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.models.intelligence'`

- [ ] **Step 3: Create the models**

```python
# src/models/intelligence.py
"""Intelligence mesh models — unified context for multi-source reasoning."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from src.models.market import EventCalendar, MarketContext
from src.models.position import Position
from src.models.signals import AlphaSignal
from src.models.analysis import SmartStrike


@dataclass
class QuantIntelligence:
    """Quantitative signal intelligence for a symbol."""
    signals: list[AlphaSignal]
    signal_count: int
    avg_strength: float
    iv_rank: float
    iv_percentile: float
    rsi: float | None
    price_vs_support: dict[str, float]
    trend_direction: str  # "uptrend" / "downtrend" / "range"


@dataclass
class TechnicalConsensus:
    """TradingView technical analysis consensus."""
    source: str  # "tradingview"
    overall: str  # STRONG_BUY / BUY / NEUTRAL / SELL / STRONG_SELL
    oscillators: str
    moving_averages: str
    buy_count: int
    neutral_count: int
    sell_count: int
    raw_indicators: dict[str, float] = field(default_factory=dict)


@dataclass
class OptionsIntelligence:
    """Real options chain intelligence for a symbol."""
    best_strike: SmartStrike | None
    iv_rank: float
    premium_yield: float
    annualized_yield: float
    bid_ask_spread_pct: float
    chain_available: bool


@dataclass
class PortfolioContext:
    """Portfolio-level context for a symbol."""
    existing_exposure_pct: float
    existing_positions: list[Position]
    account_recommendation: str
    wash_sale_blocked: bool
    earnings_conflict: bool
    available_capital: Decimal


@dataclass
class IntelligenceContext:
    """Unified intelligence context — one per symbol per analysis cycle."""
    symbol: str
    quant: QuantIntelligence
    technical_consensus: TechnicalConsensus | None
    options: OptionsIntelligence | None
    portfolio: PortfolioContext
    market: MarketContext | None = None
    events: EventCalendar | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_intelligence.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Create test fixtures**

```python
# tests/fixtures/intelligence.py
"""Factory functions for IntelligenceContext and sub-models."""
from __future__ import annotations

from decimal import Decimal

from src.models.intelligence import (
    IntelligenceContext,
    OptionsIntelligence,
    PortfolioContext,
    QuantIntelligence,
    TechnicalConsensus,
)


def make_quant_intelligence(**overrides) -> QuantIntelligence:
    defaults = dict(
        signals=[], signal_count=0, avg_strength=0.0,
        iv_rank=50.0, iv_percentile=50.0, rsi=45.0,
        price_vs_support={}, trend_direction="range",
    )
    defaults.update(overrides)
    return QuantIntelligence(**defaults)


def make_technical_consensus(**overrides) -> TechnicalConsensus:
    defaults = dict(
        source="tradingview", overall="NEUTRAL",
        oscillators="NEUTRAL", moving_averages="NEUTRAL",
        buy_count=8, neutral_count=8, sell_count=8,
        raw_indicators={},
    )
    defaults.update(overrides)
    return TechnicalConsensus(**defaults)


def make_portfolio_context(**overrides) -> PortfolioContext:
    defaults = dict(
        existing_exposure_pct=0.0, existing_positions=[],
        account_recommendation="Roth IRA", wash_sale_blocked=False,
        earnings_conflict=False, available_capital=Decimal("500000"),
    )
    defaults.update(overrides)
    return PortfolioContext(**defaults)


def make_intelligence_context(**overrides) -> IntelligenceContext:
    defaults = dict(
        symbol="NVDA",
        quant=make_quant_intelligence(),
        technical_consensus=None,
        options=None,
        portfolio=make_portfolio_context(),
        market=None,
        events=None,
    )
    defaults.update(overrides)
    return IntelligenceContext(**defaults)
```

- [ ] **Step 6: Commit**

```bash
git add src/models/intelligence.py tests/test_intelligence.py tests/fixtures/intelligence.py
git commit -m "feat: add IntelligenceContext data models for multi-source reasoning"
```

---

## Task 2: Fix IV Rank — Always Calculate HV-Proxy

**Files:**
- Modify: `src/data/market.py:26-86` (calculate_iv_rank)
- Modify: `src/data/market.py:205-209` (IV rank guard in fetch_market_context)
- Test: `tests/test_intelligence.py` (add IV rank tests)

- [ ] **Step 1: Write the failing test**

```python
# Add to tests/test_intelligence.py
from unittest.mock import patch, MagicMock
import pandas as pd
import numpy as np


class TestIVRankFix:
    def test_iv_rank_uses_hv_fallback_when_no_current_iv(self) -> None:
        """calculate_iv_rank should use HV as proxy when current_iv=0."""
        from src.data.market import calculate_iv_rank

        # Create fake 1-year history with known volatility pattern
        dates = pd.date_range("2025-04-01", periods=252, freq="B")
        # Prices that give moderate realized vol
        np.random.seed(42)
        returns = np.random.normal(0.0005, 0.015, 252)
        prices = 100 * np.exp(np.cumsum(returns))
        hist = pd.DataFrame({"Close": prices}, index=dates)

        with patch("yfinance.Ticker") as mock_ticker:
            mock_ticker.return_value.history.return_value = hist
            result = calculate_iv_rank("TEST", current_iv=0.0)

        # Should NOT return the default 50.0 — should calculate from HV
        assert result["iv_rank"] != 0.0
        assert result["hv_30d"] > 0.0
        assert 0.0 <= result["iv_rank"] <= 100.0

    def test_market_context_always_has_iv_rank(self) -> None:
        """fetch_market_context should never return iv_rank=0 for valid symbols."""
        from src.data.market import fetch_market_context

        # Mock yfinance to return reasonable data
        dates = pd.date_range("2025-04-07", periods=5, freq="B")
        prices = [130.0, 128.0, 132.0, 131.0, 133.0]
        hist_5d = pd.DataFrame({"Close": prices}, index=dates)

        dates_1y = pd.date_range("2025-04-01", periods=252, freq="B")
        np.random.seed(42)
        returns = np.random.normal(0.0005, 0.015, 252)
        prices_1y = 130 * np.exp(np.cumsum(returns))
        hist_1y = pd.DataFrame({"Close": prices_1y}, index=dates_1y)

        with patch("yfinance.Ticker") as mock_ticker:
            instance = MagicMock()
            instance.history.side_effect = [hist_5d, hist_1y, hist_1y]
            mock_ticker.return_value = instance

            # Patch VIX fetch to avoid side effects
            with patch("src.data.market.yf.Ticker") as mock_yf:
                mock_yf.return_value = instance
                mkt = fetch_market_context("PLTR", current_iv=0.0)

        assert mkt.iv_rank > 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_intelligence.py::TestIVRankFix -v`
Expected: FAIL — `iv_rank` is 0.0 because the guard skips calculation

- [ ] **Step 3: Fix calculate_iv_rank to use HV fallback**

In `src/data/market.py`, modify `calculate_iv_rank()` (lines 26-86). Change the function to use `hv_30d` as the current value when `current_iv=0`:

Replace lines 66-76 (the section after `rolling_rv = rolling_rv.dropna()`):

```python
    rv_min = float(rolling_rv.min())
    rv_max = float(rolling_rv.max())
    hv_30d = float(rolling_rv.iloc[-1])

    # Use broker-supplied IV if available, otherwise use HV as proxy
    if current_iv > 0:
        iv_as_pct = current_iv * 100
    else:
        iv_as_pct = hv_30d  # HV-based proxy

    # IV Rank
    if rv_max - rv_min > 0:
        iv_rank = (iv_as_pct - rv_min) / (rv_max - rv_min) * 100
    else:
        iv_rank = 50.0

    # IV Percentile
    iv_pctile = float((rolling_rv < iv_as_pct).sum()) / len(rolling_rv) * 100
```

- [ ] **Step 4: Remove the IV rank guard in fetch_market_context**

In `src/data/market.py`, replace lines 205-209:

```python
    # IV rank — always calculate, using HV proxy if broker IV unavailable
    iv_data = calculate_iv_rank(symbol, current_iv)
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_intelligence.py::TestIVRankFix -v`
Expected: PASS

Run: `uv run pytest tests/test_analysis.py -v`
Expected: All existing tests still PASS

- [ ] **Step 6: Commit**

```bash
git add src/data/market.py tests/test_intelligence.py
git commit -m "fix: always calculate IV rank using HV proxy when broker IV unavailable"
```

---

## Task 3: OptionContract Model + Real Options Chain

**Files:**
- Modify: `src/models/market.py:106-128` (extend OptionsChain)
- Modify: `src/data/market.py` (add fetch_options_chain)
- Modify: `tests/fixtures/market_data.py:80-99` (update make_options_chain)
- Test: `tests/test_intelligence.py`

- [ ] **Step 1: Write the failing test**

```python
# Add to tests/test_intelligence.py
import pandas as pd
from src.models.market import OptionContract, OptionsChain


class TestOptionsChain:
    def test_option_contract_creation(self) -> None:
        from datetime import date
        oc = OptionContract(
            strike=Decimal("125.00"),
            expiration=date(2026, 5, 15),
            option_type="put",
            bid=Decimal("1.45"),
            ask=Decimal("1.52"),
            mid=Decimal("1.485"),
            volume=1500,
            open_interest=8200,
            implied_vol=0.42,
            delta=-0.25,
        )
        assert oc.bid == Decimal("1.45")
        assert oc.delta == -0.25

    def test_options_chain_with_contracts(self) -> None:
        chain = OptionsChain(
            symbol="PLTR",
            puts=[],
            calls=[],
        )
        assert chain.puts == []
        assert chain.symbol == "PLTR"

    def test_fetch_options_chain_returns_populated_chain(self) -> None:
        """fetch_options_chain should return OptionsChain with puts/calls from yfinance."""
        from unittest.mock import patch, MagicMock
        from datetime import date, timedelta
        from src.data.market import fetch_options_chain

        exp_date = (date.today() + timedelta(days=30)).isoformat()
        mock_puts = pd.DataFrame({
            "strike": [120.0, 125.0, 130.0],
            "bid": [1.20, 1.80, 2.50],
            "ask": [1.30, 1.90, 2.60],
            "volume": [500, 1200, 800],
            "openInterest": [3000, 8000, 5000],
            "impliedVolatility": [0.38, 0.42, 0.45],
        })
        mock_calls = pd.DataFrame({
            "strike": [135.0, 140.0],
            "bid": [2.10, 1.50],
            "ask": [2.20, 1.60],
            "volume": [600, 400],
            "openInterest": [4000, 2000],
            "impliedVolatility": [0.35, 0.33],
        })

        mock_chain = MagicMock()
        mock_chain.puts = mock_puts
        mock_chain.calls = mock_calls

        mock_ticker = MagicMock()
        mock_ticker.options = [exp_date]
        mock_ticker.option_chain.return_value = mock_chain
        mock_ticker.history.return_value = pd.DataFrame({"Close": [131.0]})

        with patch("src.data.market.yf.Ticker", return_value=mock_ticker):
            result = fetch_options_chain("PLTR")

        assert len(result.puts) == 3
        assert len(result.calls) == 2
        assert result.puts[0].strike == Decimal("120.0")
        assert result.atm_iv is not None

    def test_fetch_options_chain_graceful_on_empty(self) -> None:
        """fetch_options_chain should return empty chain when no expirations."""
        from unittest.mock import patch, MagicMock
        from src.data.market import fetch_options_chain

        mock_ticker = MagicMock()
        mock_ticker.options = []

        with patch("src.data.market.yf.Ticker", return_value=mock_ticker):
            result = fetch_options_chain("FAKE")

        assert result.puts == []
        assert result.calls == []
        assert result.symbol == "FAKE"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_intelligence.py::TestOptionsChain -v`
Expected: FAIL — `ImportError: cannot import name 'OptionContract'`

- [ ] **Step 3: Add OptionContract and extend OptionsChain**

In `src/models/market.py`, add `OptionContract` before `OptionsChain` and extend the chain:

```python
@dataclass
class OptionContract:
    """A single option contract from a real chain."""
    strike: Decimal
    expiration: date
    option_type: str  # "put" or "call"
    bid: Decimal
    ask: Decimal
    mid: Decimal
    volume: int
    open_interest: int
    implied_vol: float
    delta: float


@dataclass
class OptionsChain:
    """Options chain data for a symbol."""
    symbol: str
    puts: list[OptionContract] = field(default_factory=list)
    calls: list[OptionContract] = field(default_factory=list)
    atm_iv: float | None = None
    historical_skew_25d: float | None = None
    iv_by_expiry: dict[str, float] = field(default_factory=dict)
    expirations: list[date] = field(default_factory=list)

    def get_iv_at_delta(self, delta: float) -> float | None:
        """Look up IV from real chain at nearest delta."""
        contracts = self.puts if delta < 0 else self.calls
        if not contracts:
            return None
        nearest = min(contracts, key=lambda c: abs(c.delta - delta))
        return nearest.implied_vol if abs(nearest.delta - delta) < 0.10 else None

    def get_expiry_near_dte(self, target_dte: int) -> date | None:
        """Find the expiration closest to target DTE."""
        if not self.expirations:
            return None
        today = date.today()
        return min(
            self.expirations,
            key=lambda d: abs((d - today).days - target_dte),
        )
```

- [ ] **Step 4: Add fetch_options_chain to src/data/market.py**

First, add `OptionContract` to the existing top-level imports in `src/data/market.py`:

```python
from src.models.market import OptionContract, MarketContext, OptionsChain, PriceHistory, EventCalendar
```

Then add this function after the existing `fetch_market_context`:

```python
def fetch_options_chain(symbol: str, target_dte: int = 30) -> OptionsChain:
    """Fetch real options chain from yfinance for nearest monthly expiration."""
    ticker = yf.Ticker(symbol)
    try:
        expirations_str = ticker.options
    except Exception:
        logger.warning("options_chain_unavailable", symbol=symbol)
        return OptionsChain(symbol=symbol)

    if not expirations_str:
        return OptionsChain(symbol=symbol)

    # Parse expiration dates and find nearest to target DTE
    today = date.today()
    exp_dates = [date.fromisoformat(e) for e in expirations_str]
    target_date = today + timedelta(days=target_dte)
    best_exp = min(exp_dates, key=lambda d: abs((d - target_date).days))

    try:
        chain = ticker.option_chain(best_exp.isoformat())
    except Exception as e:
        logger.warning("options_chain_fetch_failed", symbol=symbol, error=str(e))
        return OptionsChain(symbol=symbol, expirations=exp_dates)

    def _parse_contracts(df: Any, option_type: str) -> list[OptionContract]:
        contracts = []
        for _, row in df.iterrows():
            try:
                contracts.append(OptionContract(
                    strike=Decimal(str(round(float(row["strike"]), 2))),
                    expiration=best_exp,
                    option_type=option_type,
                    bid=Decimal(str(round(float(row.get("bid", 0)), 2))),
                    ask=Decimal(str(round(float(row.get("ask", 0)), 2))),
                    mid=Decimal(str(round((float(row.get("bid", 0)) + float(row.get("ask", 0))) / 2, 2))),
                    volume=int(row.get("volume", 0) or 0),
                    open_interest=int(row.get("openInterest", 0) or 0),
                    implied_vol=float(row.get("impliedVolatility", 0) or 0),
                    delta=0.0,  # yfinance doesn't provide delta; estimate later
                ))
            except (ValueError, KeyError):
                continue
        return contracts

    puts = _parse_contracts(chain.puts, "put")
    calls = _parse_contracts(chain.calls, "call")

    # ATM IV from nearest-to-money put
    atm_iv = None
    if puts:
        hist = ticker.history(period="1d")
        if not hist.empty:
            current_price = float(hist["Close"].iloc[-1])
            atm_put = min(puts, key=lambda c: abs(float(c.strike) - current_price))
            atm_iv = atm_put.implied_vol

    return OptionsChain(
        symbol=symbol,
        puts=puts,
        calls=calls,
        atm_iv=atm_iv,
        expirations=exp_dates,
    )
```

- [ ] **Step 5: Update make_options_chain fixture**

In `tests/fixtures/market_data.py`, update `make_options_chain()` to include the new fields with backward-compatible defaults:

```python
def make_options_chain(**overrides) -> OptionsChain:
    defaults = dict(
        symbol="NVDA",
        puts=[],
        calls=[],
        atm_iv=0.30,
        historical_skew_25d=0.05,
        iv_by_expiry={"front_month": 0.45, "second_month": 0.40},
        expirations=[],
    )
    defaults.update(overrides)
    return OptionsChain(**defaults)
```

- [ ] **Step 6: Update find_smart_strikes to prefer real chain data**

In `src/analysis/strikes.py`, modify the premium estimation section (lines 63-66) inside the `for level_price, reason in levels:` loop. The change: check if the chain has real put contracts near this strike, and if so, use real bid/ask instead of the estimation formula.

Replace lines 63-66 (the premium estimation block) with:

```python
        # Prefer real chain data when available; fall back to estimation
        real_contract = None
        if chain.puts and direction == "sell_put":
            near = [p for p in chain.puts if abs(float(p.strike) - lp) < 1.0]
            if near:
                real_contract = near[0]
        elif chain.calls and direction != "sell_put":
            near = [c for c in chain.calls if abs(float(c.strike) - lp) < 1.0]
            if near:
                real_contract = near[0]

        if real_contract and real_contract.bid > 0:
            est_premium = real_contract.mid
            est_delta = abs(real_contract.delta) if real_contract.delta != 0 else est_delta
        else:
            # Estimate premium (rough: ATM IV * sqrt(DTE/365) * strike * delta-proxy)
            atm_iv = chain.atm_iv or 0.30
            time_factor = (target_dte / 365.0) ** 0.5
            est_premium = Decimal(str(round(lp * atm_iv * time_factor * est_delta * 0.5, 2)))
```

- [ ] **Step 7: Run all tests**

Run: `uv run pytest tests/test_intelligence.py tests/test_analysis.py -v`
Expected: All PASS

- [ ] **Step 8: Commit**

```bash
git add src/models/market.py src/data/market.py src/analysis/strikes.py tests/fixtures/market_data.py tests/test_intelligence.py
git commit -m "feat: add OptionContract model, real yfinance chain fetcher, prefer real data in strikes"
```

---

## Task 4: TradingView Intelligence Layer

**Files:**
- Create: `src/data/tradingview.py`
- Modify: `pyproject.toml` (add tradingview-ta)
- Test: `tests/test_intelligence.py`

- [ ] **Step 1: Add tradingview-ta dependency**

```bash
uv add tradingview-ta
```

- [ ] **Step 2: Write the failing test**

```python
# Add to tests/test_intelligence.py
from src.data.tradingview import fetch_tradingview_consensus


class TestTradingView:
    def test_fetch_returns_technical_consensus(self) -> None:
        """Should return a TechnicalConsensus with valid fields."""
        from unittest.mock import patch, MagicMock

        mock_handler = MagicMock()
        mock_handler.get_analysis.return_value = MagicMock(
            summary={"RECOMMENDATION": "BUY", "BUY": 12, "NEUTRAL": 6, "SELL": 8},
            oscillators={"RECOMMENDATION": "NEUTRAL", "BUY": 3, "NEUTRAL": 5, "SELL": 3},
            moving_averages={"RECOMMENDATION": "BUY", "BUY": 9, "NEUTRAL": 1, "SELL": 2},
            indicators={},
        )

        with patch("src.data.tradingview.TA_Handler", return_value=mock_handler):
            result = fetch_tradingview_consensus("NVDA")

        assert result is not None
        assert result.overall == "BUY"
        assert result.buy_count == 12
        assert result.source == "tradingview"

    def test_fetch_returns_none_on_failure(self) -> None:
        """Should return None gracefully on HTTP error."""
        from unittest.mock import patch

        with patch("src.data.tradingview.TA_Handler", side_effect=Exception("HTTP 429")):
            result = fetch_tradingview_consensus("FAKE")

        assert result is None

    def test_cache_returns_same_result(self) -> None:
        """Should cache results for 1 hour."""
        from unittest.mock import patch, MagicMock

        mock_handler = MagicMock()
        mock_handler.get_analysis.return_value = MagicMock(
            summary={"RECOMMENDATION": "SELL", "BUY": 4, "NEUTRAL": 5, "SELL": 17},
            oscillators={"RECOMMENDATION": "SELL", "BUY": 1, "NEUTRAL": 2, "SELL": 8},
            moving_averages={"RECOMMENDATION": "SELL", "BUY": 3, "NEUTRAL": 3, "SELL": 9},
            indicators={},
        )

        with patch("src.data.tradingview.TA_Handler", return_value=mock_handler) as mock_cls:
            from src.data.tradingview import _tv_cache
            _tv_cache.pop("CACHE_TEST_SYM", None)  # ensure clean state for this symbol
            result1 = fetch_tradingview_consensus("CACHE_TEST_SYM")
            result2 = fetch_tradingview_consensus("CACHE_TEST_SYM")

        assert result1 is not None
        assert result1.overall == result2.overall
        # TA_Handler should only be constructed once (cache hit on second call)
        assert mock_cls.call_count == 1
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_intelligence.py::TestTradingView -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.data.tradingview'`

- [ ] **Step 4: Implement the TradingView fetcher**

```python
# src/data/tradingview.py
"""TradingView technical analysis consensus via tradingview-ta.

Fetches the same buy/sell/neutral summary millions of traders see.
Results cached with 1-hour TTL to avoid rate limiting (unofficial API).
"""
from __future__ import annotations

import time

import structlog
from tradingview_ta import TA_Handler, Interval

from src.models.intelligence import TechnicalConsensus

logger = structlog.get_logger()

# Simple TTL cache: {symbol: (timestamp, TechnicalConsensus)}
_tv_cache: dict[str, tuple[float, TechnicalConsensus]] = {}
_CACHE_TTL = 3600  # 1 hour


def fetch_tradingview_consensus(symbol: str) -> TechnicalConsensus | None:
    """Fetch TradingView technical analysis for a US stock.

    Returns None on any failure (rate limit, network, bad symbol).
    Cached for 1 hour per symbol.
    """
    now = time.time()
    if symbol in _tv_cache:
        ts, cached = _tv_cache[symbol]
        if now - ts < _CACHE_TTL:
            return cached

    try:
        handler = TA_Handler(
            symbol=symbol,
            screener="america",
            exchange="NASDAQ",
            interval=Interval.INTERVAL_1_DAY,
        )
        analysis = handler.get_analysis()

        summary = analysis.summary
        oscillators = analysis.oscillators
        moving_averages = analysis.moving_averages
        indicators = analysis.indicators or {}

        result = TechnicalConsensus(
            source="tradingview",
            overall=summary.get("RECOMMENDATION", "NEUTRAL"),
            oscillators=oscillators.get("RECOMMENDATION", "NEUTRAL"),
            moving_averages=moving_averages.get("RECOMMENDATION", "NEUTRAL"),
            buy_count=int(summary.get("BUY", 0)),
            neutral_count=int(summary.get("NEUTRAL", 0)),
            sell_count=int(summary.get("SELL", 0)),
            raw_indicators={k: float(v) for k, v in indicators.items()
                           if isinstance(v, (int, float)) and v is not None},
        )

        _tv_cache[symbol] = (now, result)
        logger.info("tradingview_fetched", symbol=symbol, overall=result.overall)
        return result

    except Exception as e:
        logger.warning("tradingview_failed", symbol=symbol, error=str(e))
        return None
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_intelligence.py::TestTradingView -v`
Expected: All 3 PASS

- [ ] **Step 6: Commit**

```bash
git add src/data/tradingview.py tests/test_intelligence.py pyproject.toml uv.lock
git commit -m "feat: add TradingView technical consensus fetcher with 1-hour TTL cache"
```

---

## Task 5: Intelligence Builder — Assemble Context Per Symbol

**Files:**
- Create: `src/intelligence/__init__.py`
- Create: `src/intelligence/builder.py`
- Test: `tests/test_intelligence.py`

- [ ] **Step 1: Write the failing test**

```python
# Add to tests/test_intelligence.py
from src.intelligence.builder import build_intelligence_context
from tests.fixtures.market_data import make_market_context, make_price_history, make_options_chain, make_event_calendar
from tests.fixtures.trades import make_alpha_signal


class TestIntelligenceBuilder:
    def test_builds_context_with_signals(self) -> None:
        from src.models.enums import SignalType
        signals = [
            make_alpha_signal(symbol="NVDA", strength=70),
            make_alpha_signal(symbol="NVDA", strength=65, signal_type=SignalType.IV_RANK_SPIKE),
        ]
        mkt = make_market_context(iv_rank=62.0)
        hist = make_price_history(rsi_14=28.0)
        chain = make_options_chain()
        cal = make_event_calendar()

        ctx = build_intelligence_context(
            symbol="NVDA",
            signals=signals,
            market=mkt,
            price_history=hist,
            chain=chain,
            calendar=cal,
        )

        assert ctx.symbol == "NVDA"
        assert ctx.quant.signal_count == 2
        assert ctx.quant.avg_strength == 67.5
        assert ctx.quant.rsi == 28.0
        assert ctx.quant.iv_rank == 62.0
        assert ctx.market is not None

    def test_builds_context_without_tradingview(self) -> None:
        """Should work when TradingView is unavailable."""
        ctx = build_intelligence_context(
            symbol="FAKE",
            signals=[],
            market=make_market_context(),
            price_history=make_price_history(),
            chain=make_options_chain(),
            calendar=make_event_calendar(),
            technical_consensus=None,
        )
        assert ctx.technical_consensus is None
        assert ctx.quant.signal_count == 0

    def test_trend_direction_from_moving_averages(self) -> None:
        """Downtrend when price below both 50 and 200 SMA."""
        hist = make_price_history(
            current_price=Decimal("130"),
            sma_50=Decimal("145"),
            sma_200=Decimal("160"),
        )
        ctx = build_intelligence_context(
            symbol="PLTR",
            signals=[],
            market=make_market_context(),
            price_history=hist,
            chain=make_options_chain(),
            calendar=make_event_calendar(),
        )
        assert ctx.quant.trend_direction == "downtrend"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_intelligence.py::TestIntelligenceBuilder -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.intelligence'`

- [ ] **Step 3: Implement the builder**

```python
# src/intelligence/__init__.py
"""Intelligence mesh — multi-source context assembly."""
from src.intelligence.builder import build_intelligence_context

__all__ = ["build_intelligence_context"]
```

```python
# src/intelligence/builder.py
"""Assemble IntelligenceContext from all available sources."""
from __future__ import annotations

from decimal import Decimal

from src.models.intelligence import (
    IntelligenceContext,
    OptionsIntelligence,
    PortfolioContext,
    QuantIntelligence,
    TechnicalConsensus,
)
from src.models.analysis import SmartStrike
from src.models.market import EventCalendar, MarketContext, OptionsChain, PriceHistory
from src.models.position import PortfolioState, Position
from src.models.signals import AlphaSignal


def build_intelligence_context(
    symbol: str,
    signals: list[AlphaSignal],
    market: MarketContext,
    price_history: PriceHistory,
    chain: OptionsChain,
    calendar: EventCalendar,
    technical_consensus: TechnicalConsensus | None = None,
    portfolio_state: PortfolioState | None = None,
) -> IntelligenceContext:
    """Build a unified IntelligenceContext for one symbol."""
    # Quant intelligence
    avg_strength = (
        sum(s.strength for s in signals) / len(signals) if signals else 0.0
    )
    trend = _classify_trend(price_history)
    support_distances = _calculate_support_distances(price_history)

    quant = QuantIntelligence(
        signals=signals,
        signal_count=len(signals),
        avg_strength=avg_strength,
        iv_rank=market.iv_rank,
        iv_percentile=market.iv_percentile,
        rsi=price_history.rsi_14,
        price_vs_support=support_distances,
        trend_direction=trend,
    )

    # Options intelligence
    options = None
    if chain.puts:
        best = _find_best_put(chain, price_history)
        if best:
            bid_ask_spread = (
                float(best.ask - best.bid) / float(best.mid) * 100
                if best.mid > 0 else 0.0
            )
            capital = float(best.strike) * 100
            yield_on_cap = float(best.mid) * 100 / capital if capital > 0 else 0.0
            ann_yield = yield_on_cap * (365.0 / 30.0)

            options = OptionsIntelligence(
                best_strike=SmartStrike(
                    strike=best.strike,
                    delta=best.delta,
                    premium=best.mid,
                    yield_on_capital=round(yield_on_cap, 4),
                    annualized_yield=round(ann_yield, 4),
                    technical_reason=None,
                ),
                iv_rank=market.iv_rank,
                premium_yield=round(yield_on_cap, 4),
                annualized_yield=round(ann_yield, 4),
                bid_ask_spread_pct=round(bid_ask_spread, 2),
                chain_available=True,
            )

    # Portfolio context
    existing_positions: list[Position] = []
    exposure_pct = 0.0
    available_capital = Decimal("0")
    if portfolio_state:
        existing_positions = [p for p in portfolio_state.positions if p.symbol == symbol]
        exposure_pct = portfolio_state.concentration.get(symbol, 0.0)
        available_capital = portfolio_state.buying_power

    portfolio = PortfolioContext(
        existing_exposure_pct=exposure_pct,
        existing_positions=existing_positions,
        account_recommendation="",  # filled by account routing later
        wash_sale_blocked=False,  # filled by wash sale tracker later
        earnings_conflict=False,  # filled by calendar check later
        available_capital=available_capital,
    )

    return IntelligenceContext(
        symbol=symbol,
        quant=quant,
        technical_consensus=technical_consensus,
        options=options,
        portfolio=portfolio,
        market=market,
        events=calendar,
    )


def _classify_trend(hist: PriceHistory) -> str:
    """Classify trend from SMA positions."""
    price = float(hist.current_price) if hist.current_price else 0.0
    if price <= 0:
        return "range"

    below_50 = hist.sma_50 is not None and price < float(hist.sma_50)
    below_200 = hist.sma_200 is not None and price < float(hist.sma_200)
    above_50 = hist.sma_50 is not None and price > float(hist.sma_50)
    above_200 = hist.sma_200 is not None and price > float(hist.sma_200)

    if below_50 and below_200:
        return "downtrend"
    if above_50 and above_200:
        return "uptrend"
    return "range"


def _calculate_support_distances(hist: PriceHistory) -> dict[str, float]:
    """Calculate % distance from current price to each support level."""
    price = float(hist.current_price) if hist.current_price else 0.0
    if price <= 0:
        return {}

    distances: dict[str, float] = {}
    if hist.sma_200:
        distances["200 SMA"] = round((price - float(hist.sma_200)) / price * 100, 1)
    if hist.sma_50:
        distances["50 SMA"] = round((price - float(hist.sma_50)) / price * 100, 1)
    if hist.low_52w:
        distances["52w Low"] = round((price - float(hist.low_52w)) / price * 100, 1)
    return distances


def _find_best_put(chain: OptionsChain, hist: PriceHistory) -> object | None:
    """Find the best OTM put from the chain (nearest to 0.25-0.30 delta range)."""
    if not chain.puts:
        return None

    price = float(hist.current_price) if hist.current_price else 0.0
    if price <= 0:
        return None

    # Filter to OTM puts (strike below current price)
    otm_puts = [p for p in chain.puts if float(p.strike) < price and p.bid > 0]
    if not otm_puts:
        return None

    # Pick the one closest to 5-10% OTM (good balance of premium and safety)
    target_strike = price * 0.93
    return min(otm_puts, key=lambda p: abs(float(p.strike) - target_strike))
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_intelligence.py::TestIntelligenceBuilder -v`
Expected: All 3 PASS

- [ ] **Step 5: Commit**

```bash
git add src/intelligence/__init__.py src/intelligence/builder.py tests/test_intelligence.py
git commit -m "feat: add intelligence builder to assemble multi-source context per symbol"
```

---

## Task 6: Claude Reasoning Engine

**Files:**
- Create: `src/delivery/reasoning.py`
- Test: `tests/test_intelligence.py`

- [ ] **Step 1: Write the failing test**

```python
# Add to tests/test_intelligence.py
from src.delivery.reasoning import build_reasoning_prompt


class TestClaudeReasoning:
    def test_build_prompt_includes_all_sections(self) -> None:
        from tests.fixtures.intelligence import (
            make_intelligence_context,
            make_quant_intelligence,
            make_technical_consensus,
        )
        ctx = make_intelligence_context(
            quant=make_quant_intelligence(signal_count=2, avg_strength=65, rsi=28.0),
            technical_consensus=make_technical_consensus(overall="SELL"),
        )
        prompt = build_reasoning_prompt([ctx])
        assert "NVDA" in prompt
        assert "QUANT SIGNALS" in prompt
        assert "TRADINGVIEW" in prompt
        assert "SELL" in prompt

    def test_build_prompt_handles_missing_tradingview(self) -> None:
        from tests.fixtures.intelligence import make_intelligence_context
        ctx = make_intelligence_context(technical_consensus=None)
        prompt = build_reasoning_prompt([ctx])
        assert "TradingView: unavailable" in prompt

    def test_build_prompt_caps_at_5_symbols(self) -> None:
        from tests.fixtures.intelligence import make_intelligence_context
        contexts = [make_intelligence_context(symbol=f"SYM{i}") for i in range(8)]
        prompt = build_reasoning_prompt(contexts)
        # Should only include first 5
        assert "SYM0" in prompt
        assert "SYM4" in prompt
        assert "SYM5" not in prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_intelligence.py::TestClaudeReasoning -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.delivery.reasoning'`

- [ ] **Step 3: Implement the reasoning engine**

```python
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
            lines.append("TRADINGVIEW: TradingView: unavailable")

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


async def generate_analyst_brief(
    contexts: list[IntelligenceContext],
    regime_summary: str = "",
) -> str | None:
    """Call Claude API to generate reasoned analyst brief.

    Returns None if API key is missing or call fails.
    """
    try:
        import anthropic
    except ImportError:
        logger.info("anthropic_not_installed")
        return None

    import os
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.info("no_anthropic_api_key")
        return None

    user_prompt = build_reasoning_prompt(contexts)
    if regime_summary:
        user_prompt = f"REGIME: {regime_summary}\n\n{user_prompt}"

    try:
        client = anthropic.AsyncAnthropic(api_key=api_key)
        response = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return response.content[0].text
    except Exception as e:
        logger.warning("claude_reasoning_failed", error=str(e))
        return None
```

- [ ] **Step 4: Update src/delivery/__init__.py exports**

Add the new public functions to `src/delivery/__init__.py`:

```python
from src.delivery.reasoning import build_reasoning_prompt, generate_analyst_brief
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_intelligence.py::TestClaudeReasoning -v`
Expected: All 3 PASS

- [ ] **Step 6: Commit**

```bash
git add src/delivery/reasoning.py src/delivery/__init__.py tests/test_intelligence.py
git commit -m "feat: add Claude reasoning engine with prompt builder and analyst brief generation"
```

---

## Task 7: Wire Intelligence Mesh Into Briefing Pipeline

**Files:**
- Modify: `src/main.py:140-370` (run_analysis_cycle, format_local_briefing)
- Test: manual run with `uv run python -m src.main --mode briefing`

- [ ] **Step 1: Update imports in main.py**

Add to the imports section at the top of `src/main.py`:

```python
from src.data.tradingview import fetch_tradingview_consensus
from src.intelligence.builder import build_intelligence_context
from src.delivery.reasoning import generate_analyst_brief
from src.models.intelligence import IntelligenceContext
```

- [ ] **Step 2: Update run_analysis_cycle to build intelligence contexts**

Replace the pipeline in `run_analysis_cycle()` (after signal detection, before risk checks). The new pipeline:

1. Existing: Fetch VIX/SPY, classify regime
2. Existing: Fetch per-symbol data
3. **New**: Fetch TradingView consensus per symbol
4. Existing: Detect quant signals
5. **New**: Build IntelligenceContext per symbol
6. **New**: Generate Claude analyst brief
7. Existing: Risk checks
8. Existing: Format and print briefing

Update the section after signal detection to:

```python
    # 5. Build intelligence contexts
    intel_contexts: list[IntelligenceContext] = []
    by_symbol_signals: dict[str, list[AlphaSignal]] = {}
    for s in all_signals:
        by_symbol_signals.setdefault(s.symbol, []).append(s)

    for symbol, mkt, hist, chain, cal in watchlist_data:
        # TradingView consensus (cached, graceful failure)
        tv_consensus = fetch_tradingview_consensus(symbol)

        ctx = build_intelligence_context(
            symbol=symbol,
            signals=by_symbol_signals.get(symbol, []),
            market=mkt,
            price_history=hist,
            chain=chain,
            calendar=cal,
            technical_consensus=tv_consensus,
        )
        intel_contexts.append(ctx)

    # 6. Claude analyst brief (opt-in, requires ANTHROPIC_API_KEY)
    analyst_brief = None
    contexts_with_signals = [c for c in intel_contexts if c.quant.signal_count > 0]
    if contexts_with_signals:
        regime_str = f"{regime.regime.upper()} — VIX {vix:.1f}, SPY {spy_change:+.2%}"
        analyst_brief = await generate_analyst_brief(contexts_with_signals, regime_str)
```

- [ ] **Step 3: Update format_local_briefing signature and add new sections**

Update the function signature to accept new parameters:

```python
def format_local_briefing(
    regime: RegimeState,
    vix: float,
    spy_change: float,
    all_signals: list[AlphaSignal],
    watchlist_data: list[tuple[str, MarketContext, PriceHistory, OptionsChain, EventCalendar]],
    tax_alerts: list[str],
    recommendations: list[SizedOpportunity] | None = None,
    intel_contexts: list[IntelligenceContext] | None = None,
    analyst_brief: str | None = None,
) -> str:
```

**Remove** the three standalone sections that are now subsumed by the analyst brief. Delete the following blocks from `format_local_briefing`:

1. The "DIP OPPORTUNITIES" section (lines 301-308 in current main.py): everything from `# Dip opportunities` through the `lines.append(f"  {sym}: {mkt.price_change_1d:+.1f}%` block.
2. The "NEAR SUPPORT" section (lines 311-326): everything from `# Support proximity` through the `near_support` loop.
3. The "OVERSOLD" section (lines 329-334): everything from `# Oversold` through the `oversold` loop.

**Add** these two new sections after the ACTION PLAN section and before SIGNAL FLASH:

```python
    # Analyst brief (Claude-powered reasoning)
    if analyst_brief:
        lines.append(f"\n━━ ANALYST BRIEF ━━")
        lines.append(analyst_brief)
    
    # TradingView consensus summary
    if intel_contexts:
        tv_available = [c for c in intel_contexts if c.technical_consensus]
        if tv_available:
            lines.append(f"\n━━ TRADINGVIEW CONSENSUS ━━")
            for ctx in tv_available:
                tc = ctx.technical_consensus
                agreement = ""
                if ctx.quant.signal_count > 0:
                    quant_bullish = ctx.quant.avg_strength > 50
                    tv_bullish = tc.overall in ("BUY", "STRONG_BUY")
                    if quant_bullish == tv_bullish:
                        agreement = " [AGREES with signals]"
                    else:
                        agreement = " [DISSENTS from signals]"
                lines.append(
                    f"  {ctx.symbol}: {tc.overall} "
                    f"({tc.buy_count}B/{tc.neutral_count}N/{tc.sell_count}S) "
                    f"| MA: {tc.moving_averages} | Osc: {tc.oscillators}"
                    f"{agreement}"
                )
```

- [ ] **Step 4: Pass new data through the pipeline**

Update the `format_local_briefing()` call in `run_analysis_cycle()` to pass the new intel contexts and analyst brief:

```python
    briefing = format_local_briefing(
        regime=regime,
        vix=vix,
        spy_change=spy_change,
        all_signals=all_signals,
        watchlist_data=watchlist_data,
        tax_alerts=tax_alerts,
        recommendations=recommendations,
        intel_contexts=intel_contexts,
        analyst_brief=analyst_brief,
    )
```

Note: `build_recommendations()` does NOT need changes — it operates on `all_signals` and `watchlist_data` as before. The intelligence contexts are a parallel data path for the briefing output.

- [ ] **Step 5: Write automated test for new briefing parameters**

Add to `tests/test_intelligence.py`:

```python
class TestBriefingWiring:
    def test_format_local_briefing_accepts_intel_contexts(self) -> None:
        """format_local_briefing should accept and render intel_contexts."""
        from datetime import datetime
        from src.main import format_local_briefing
        from src.monitor.regime import RegimeState
        from tests.fixtures.intelligence import make_intelligence_context, make_technical_consensus

        regime = RegimeState(
            regime="hold", vix=19.0, spy_change_pct=0.005,
            severity="normal", target_deployed=0.70, timestamp=datetime.utcnow(),
        )
        ctx = make_intelligence_context(
            symbol="NVDA",
            technical_consensus=make_technical_consensus(overall="BUY"),
        )

        # Minimal valid call with the new parameters
        briefing = format_local_briefing(
            regime=regime,
            vix=19.0,
            spy_change=0.005,
            all_signals=[],
            watchlist_data=[],
            tax_alerts=[],
            recommendations=None,
            intel_contexts=[ctx],
            analyst_brief="Test analyst brief content",
        )

        assert "TRADINGVIEW CONSENSUS" in briefing
        assert "NVDA" in briefing
        assert "BUY" in briefing
        assert "ANALYST BRIEF" in briefing
        assert "Test analyst brief content" in briefing

    def test_format_local_briefing_works_without_intel(self) -> None:
        """format_local_briefing should work with no intel contexts (backward compat)."""
        from datetime import datetime
        from src.main import format_local_briefing
        from src.monitor.regime import RegimeState

        regime = RegimeState(
            regime="attack", vix=15.0, spy_change_pct=0.01,
            severity="normal", target_deployed=0.70, timestamp=datetime.utcnow(),
        )
        briefing = format_local_briefing(
            regime=regime,
            vix=15.0,
            spy_change=0.01,
            all_signals=[],
            watchlist_data=[],
            tax_alerts=[],
        )

        assert "WHEEL COPILOT" in briefing
        assert "TRADINGVIEW CONSENSUS" not in briefing
```

- [ ] **Step 6: Run all tests**

Run: `uv run pytest tests/test_intelligence.py::TestBriefingWiring tests/test_analysis.py tests/test_scout_monitor.py -v`
Expected: All PASS

- [ ] **Step 7: Smoke test end-to-end**

Run: `source ~/.zshrc && uv run python -m src.main --mode briefing 2>&1 | head -80`
Expected: Briefing with TRADINGVIEW CONSENSUS section showing per-symbol ratings. If ANTHROPIC_API_KEY is set, ANALYST BRIEF section with Claude reasoning. If not, mechanical briefing only.

- [ ] **Step 8: Commit**

```bash
git add src/main.py tests/test_intelligence.py
git commit -m "feat: wire intelligence mesh into briefing pipeline with TradingView + Claude reasoning"
```

---

## Task 8: Portfolio Loading — AlpacaPosition Conversion

**Files:**
- Create: `src/data/portfolio.py`
- Test: `tests/test_intelligence.py`

This task implements the spec's Fix 3: loading portfolio positions from Alpaca (paper) and converting `AlpacaPosition` → `Position` so that `PortfolioContext` has real data.

- [ ] **Step 1: Write the failing test**

```python
# Add to tests/test_intelligence.py
class TestPortfolioLoading:
    def test_alpaca_position_to_position(self) -> None:
        """Convert an AlpacaPosition to the shared Position model."""
        from src.data.portfolio import alpaca_position_to_position
        from src.execution.alpaca_client import AlpacaPosition

        ap = AlpacaPosition(
            symbol="PLTR260515P00125000",
            quantity=-1,
            avg_entry_price=Decimal("1.80"),
            current_price=Decimal("2.65"),
            unrealized_pnl=Decimal("-85"),
            market_value=Decimal("265"),
        )
        pos = alpaca_position_to_position(ap)

        assert pos.symbol == "PLTR"
        assert pos.position_type == "short_put"
        assert pos.strike == Decimal("125")
        assert pos.entry_price == Decimal("1.80")
        assert pos.days_to_expiry >= 0

    def test_load_portfolio_state_from_alpaca(self) -> None:
        """load_portfolio_state should return PortfolioState with converted positions."""
        from unittest.mock import patch, MagicMock
        from src.data.portfolio import load_portfolio_state
        from src.execution.alpaca_client import AlpacaPosition, AlpacaAccountInfo

        mock_account = AlpacaAccountInfo(
            equity=Decimal("500000"),
            buying_power=Decimal("250000"),
            cash=Decimal("150000"),
            portfolio_value=Decimal("500000"),
            positions=[
                AlpacaPosition(
                    symbol="NVDA260515P00130000",
                    quantity=-2,
                    avg_entry_price=Decimal("3.20"),
                    current_price=Decimal("2.50"),
                    unrealized_pnl=Decimal("140"),
                    market_value=Decimal("500"),
                ),
            ],
        )

        mock_client = MagicMock()
        mock_client.get_account.return_value = mock_account
        with patch("src.data.portfolio.AlpacaPaperClient", return_value=mock_client):
            state = load_portfolio_state()

        assert state.buying_power == Decimal("250000")
        assert len(state.positions) == 1
        assert state.positions[0].symbol == "NVDA"
        assert state.concentration.get("NVDA", 0) > 0

    def test_load_portfolio_state_empty(self) -> None:
        """load_portfolio_state returns empty state when no positions."""
        from unittest.mock import patch, MagicMock
        from src.data.portfolio import load_portfolio_state
        from src.execution.alpaca_client import AlpacaAccountInfo

        mock_account = AlpacaAccountInfo(
            equity=Decimal("500000"),
            buying_power=Decimal("500000"),
        )

        mock_client = MagicMock()
        mock_client.get_account.return_value = mock_account
        with patch("src.data.portfolio.AlpacaPaperClient", return_value=mock_client):
            state = load_portfolio_state()

        assert state.positions == []
        assert state.buying_power == Decimal("500000")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_intelligence.py::TestPortfolioLoading -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.data.portfolio'`

- [ ] **Step 3: Implement portfolio loading**

```python
# src/data/portfolio.py
"""Portfolio loading — convert broker positions to shared Position model.

Loads positions from Alpaca (paper) or E*Trade (live), converts to
the shared Position model, and builds PortfolioState.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import structlog

from src.execution.alpaca_client import AlpacaPaperClient, AlpacaPosition
from src.models.position import PortfolioState, Position

logger = structlog.get_logger()


def alpaca_position_to_position(ap: AlpacaPosition) -> Position:
    """Convert AlpacaPosition to shared Position model.

    Parses OCC option symbol (e.g. PLTR260515P00125000) to extract
    underlying, expiration, option type, and strike.
    """
    underlying, expiration, option_type, strike = _parse_occ_symbol(ap.symbol)
    days_to_expiry = (expiration - date.today()).days if expiration else 0
    position_type = f"short_{option_type}" if ap.quantity < 0 else f"long_{option_type}"

    return Position(
        symbol=underlying,
        position_type=position_type,
        quantity=abs(ap.quantity),
        strike=strike,
        expiration=expiration,
        entry_price=ap.avg_entry_price,
        current_price=ap.current_price,
        underlying_price=Decimal("0"),  # filled by caller with market data
        cost_basis=strike * 100 if option_type == "put" else Decimal("0"),
        delta=0.0,  # filled from chain data when available
        theta=0.0,
        gamma=0.0,
        vega=0.0,
        iv=0.0,
        days_to_expiry=max(0, days_to_expiry),
        unrealized_pnl=ap.unrealized_pnl,
        market_value=ap.market_value,
        option_type=option_type,
    )


def load_portfolio_state() -> PortfolioState:
    """Load current portfolio from Alpaca and convert to PortfolioState."""
    try:
        client = AlpacaPaperClient()
        account = client.get_account()
    except Exception as e:
        logger.warning("portfolio_load_failed", error=str(e))
        return PortfolioState()

    positions = [alpaca_position_to_position(p) for p in account.positions]

    # Build concentration map: exposure per underlying as % of NLV
    nlv = float(account.equity) if account.equity > 0 else 1.0
    concentration: dict[str, float] = {}
    for pos in positions:
        capital = float(pos.strike) * 100 * pos.quantity
        concentration[pos.symbol] = concentration.get(pos.symbol, 0.0) + capital / nlv

    return PortfolioState(
        positions=positions,
        cash_available=account.cash,
        buying_power=account.buying_power,
        net_liquidation=account.equity,
        concentration=concentration,
    )


def _parse_occ_symbol(occ: str) -> tuple[str, date | None, str, Decimal]:
    """Parse OCC option symbol: PLTR260515P00125000 → (PLTR, 2026-05-15, put, 125.00)."""
    match = re.match(r"^([A-Z]+)(\d{6})([PC])(\d{8})$", occ)
    if not match:
        # Not an option symbol — treat as stock
        return occ, None, "stock", Decimal("0")

    underlying = match.group(1)
    date_str = match.group(2)
    opt_type = "put" if match.group(3) == "P" else "call"
    strike_raw = int(match.group(4))
    strike = Decimal(str(strike_raw)) / 1000

    exp = date(2000 + int(date_str[:2]), int(date_str[2:4]), int(date_str[4:6]))
    return underlying, exp, opt_type, strike
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_intelligence.py::TestPortfolioLoading -v`
Expected: All 3 PASS

- [ ] **Step 5: Commit**

```bash
git add src/data/portfolio.py tests/test_intelligence.py
git commit -m "feat: add AlpacaPosition to Position conversion and portfolio loading"
```

---

## Task 9: Position Intelligence — Hold/Watch/Close Recommendations

**Files:**
- Create: `src/intelligence/position_review.py`
- Modify: `src/intelligence/__init__.py`
- Modify: `src/main.py` (add position review to briefing)
- Test: `tests/test_intelligence.py`

This task implements Spec Component 4: the position scan that evaluates open positions and recommends HOLD / WATCH CLOSELY / TAKE PROFIT / CLOSE NOW.

- [ ] **Step 1: Write the failing test**

```python
# Add to tests/test_intelligence.py
from datetime import date


class TestPositionReview:
    def test_close_now_when_loss_stop_hit(self) -> None:
        """Should recommend CLOSE NOW when loss exceeds 2x premium."""
        from src.intelligence.position_review import review_position
        from tests.fixtures.intelligence import make_intelligence_context, make_quant_intelligence
        from src.models.position import Position

        pos = Position(
            symbol="PLTR", position_type="short_put", quantity=1,
            strike=Decimal("125"), expiration=date(2026, 5, 15),
            entry_price=Decimal("1.80"), current_price=Decimal("4.50"),
            underlying_price=Decimal("120"), cost_basis=Decimal("12500"),
            delta=-0.45, theta=0.03, gamma=0.01, vega=0.08, iv=0.55,
            days_to_expiry=32, unrealized_pnl=Decimal("-270"),
        )
        ctx = make_intelligence_context(
            symbol="PLTR",
            quant=make_quant_intelligence(trend_direction="downtrend"),
        )

        result = review_position(pos, ctx)
        assert result.action == "CLOSE NOW"
        assert "loss stop" in result.reasoning.lower()

    def test_hold_when_thesis_intact(self) -> None:
        """Should recommend HOLD when position is healthy."""
        from src.intelligence.position_review import review_position
        from tests.fixtures.intelligence import (
            make_intelligence_context, make_quant_intelligence, make_technical_consensus,
        )
        from src.models.position import Position

        pos = Position(
            symbol="NVDA", position_type="short_put", quantity=2,
            strike=Decimal("130"), expiration=date(2026, 5, 15),
            entry_price=Decimal("3.20"), current_price=Decimal("1.50"),
            underlying_price=Decimal("145"), cost_basis=Decimal("26000"),
            delta=-0.15, theta=0.05, gamma=0.01, vega=0.06, iv=0.40,
            days_to_expiry=32, unrealized_pnl=Decimal("340"),
        )
        ctx = make_intelligence_context(
            symbol="NVDA",
            quant=make_quant_intelligence(trend_direction="uptrend"),
            technical_consensus=make_technical_consensus(overall="BUY"),
        )

        result = review_position(pos, ctx)
        assert result.action == "HOLD"

    def test_take_profit_when_most_of_premium_captured(self) -> None:
        """Should recommend TAKE PROFIT when >75% of premium captured."""
        from src.intelligence.position_review import review_position
        from tests.fixtures.intelligence import make_intelligence_context, make_quant_intelligence
        from src.models.position import Position

        pos = Position(
            symbol="AAPL", position_type="short_put", quantity=1,
            strike=Decimal("200"), expiration=date(2026, 5, 15),
            entry_price=Decimal("4.00"), current_price=Decimal("0.80"),
            underlying_price=Decimal("215"), cost_basis=Decimal("20000"),
            delta=-0.08, theta=0.02, gamma=0.005, vega=0.03, iv=0.25,
            days_to_expiry=32, unrealized_pnl=Decimal("320"),
        )
        ctx = make_intelligence_context(
            symbol="AAPL",
            quant=make_quant_intelligence(trend_direction="uptrend"),
        )

        result = review_position(pos, ctx)
        assert result.action == "TAKE PROFIT"

    def test_watch_closely_when_tv_flips(self) -> None:
        """Should recommend WATCH CLOSELY when TradingView flips bearish."""
        from src.intelligence.position_review import review_position
        from tests.fixtures.intelligence import (
            make_intelligence_context, make_quant_intelligence, make_technical_consensus,
        )
        from src.models.position import Position

        pos = Position(
            symbol="META", position_type="short_put", quantity=1,
            strike=Decimal("450"), expiration=date(2026, 5, 15),
            entry_price=Decimal("6.50"), current_price=Decimal("5.00"),
            underlying_price=Decimal("460"), cost_basis=Decimal("45000"),
            delta=-0.30, theta=0.04, gamma=0.01, vega=0.10, iv=0.45,
            days_to_expiry=32, unrealized_pnl=Decimal("150"),
        )
        ctx = make_intelligence_context(
            symbol="META",
            quant=make_quant_intelligence(trend_direction="range"),
            technical_consensus=make_technical_consensus(overall="STRONG_SELL"),
        )

        result = review_position(pos, ctx)
        assert result.action == "WATCH CLOSELY"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_intelligence.py::TestPositionReview -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.intelligence.position_review'`

- [ ] **Step 3: Implement position review**

```python
# src/intelligence/position_review.py
"""Position review — evaluate open positions for hold/watch/close.

The same intelligence that decides entries continuously re-evaluates
open positions. If the system wouldn't recommend opening the trade
today, it tells you to consider closing it.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import structlog

from src.models.intelligence import IntelligenceContext
from src.models.position import Position

logger = structlog.get_logger()


@dataclass
class PositionReview:
    """Review result for a single position."""
    symbol: str
    action: str  # "CLOSE NOW" / "TAKE PROFIT" / "WATCH CLOSELY" / "HOLD"
    reasoning: str
    current_pnl: Decimal
    days_to_expiry: int


def review_position(position: Position, context: IntelligenceContext) -> PositionReview:
    """Review a single open position against current intelligence.

    Priority order:
    1. CLOSE NOW — loss stop hit, earnings conflict, intelligence consensus bearish
    2. TAKE PROFIT — captured >75% of premium
    3. WATCH CLOSELY — something changed (TV flipped, trend weakening)
    4. HOLD — thesis intact, everything healthy
    """
    pnl = position.entry_price - position.current_price  # positive = profit for short
    pnl_pct = float(pnl / position.entry_price) if position.entry_price > 0 else 0.0
    loss_multiple = float(position.current_price / position.entry_price) if position.entry_price > 0 else 0.0

    reasons: list[str] = []

    # 1. CLOSE NOW checks
    # Loss stop: current price > 2x entry price (for monthlies)
    if loss_multiple >= 2.0:
        reasons.append(f"Loss stop hit: current ${position.current_price} is "
                       f"{loss_multiple:.1f}x entry ${position.entry_price}")
        return PositionReview(
            symbol=position.symbol, action="CLOSE NOW",
            reasoning=". ".join(reasons),
            current_pnl=pnl * 100 * position.quantity,
            days_to_expiry=position.days_to_expiry,
        )

    # Earnings conflict
    if context.portfolio.earnings_conflict:
        reasons.append("Earnings within expiration window")
        return PositionReview(
            symbol=position.symbol, action="CLOSE NOW",
            reasoning=". ".join(reasons),
            current_pnl=pnl * 100 * position.quantity,
            days_to_expiry=position.days_to_expiry,
        )

    # 2. TAKE PROFIT — captured >75% of premium
    if pnl_pct >= 0.75:
        reasons.append(f"Captured {pnl_pct:.0%} of premium "
                       f"(${pnl * 100 * position.quantity:,.0f} profit)")
        return PositionReview(
            symbol=position.symbol, action="TAKE PROFIT",
            reasoning=". ".join(reasons),
            current_pnl=pnl * 100 * position.quantity,
            days_to_expiry=position.days_to_expiry,
        )

    # 3. WATCH CLOSELY checks
    watch_reasons: list[str] = []

    # TradingView flipped strongly bearish
    tc = context.technical_consensus
    if tc and tc.overall in ("SELL", "STRONG_SELL"):
        watch_reasons.append(f"TradingView consensus: {tc.overall}")

    # Trend is downtrend for a short put position
    if (context.quant.trend_direction == "downtrend"
            and position.position_type == "short_put"):
        watch_reasons.append("Price in confirmed downtrend")

    # IV dropped significantly (premium likely cheap now)
    if context.quant.iv_rank < 30 and context.quant.iv_rank > 0:
        watch_reasons.append(f"IV rank dropped to {context.quant.iv_rank:.0f}")

    if watch_reasons:
        return PositionReview(
            symbol=position.symbol, action="WATCH CLOSELY",
            reasoning=". ".join(watch_reasons),
            current_pnl=pnl * 100 * position.quantity,
            days_to_expiry=position.days_to_expiry,
        )

    # 4. HOLD — everything healthy
    hold_reasons = ["Thesis intact"]
    if tc:
        hold_reasons.append(f"TradingView: {tc.overall}")
    hold_reasons.append(f"Trend: {context.quant.trend_direction}")
    if pnl > 0:
        hold_reasons.append(f"P&L: +${pnl * 100 * position.quantity:,.0f}")

    return PositionReview(
        symbol=position.symbol, action="HOLD",
        reasoning=". ".join(hold_reasons),
        current_pnl=pnl * 100 * position.quantity,
        days_to_expiry=position.days_to_expiry,
    )


def format_position_review(reviews: list[PositionReview]) -> str:
    """Format position reviews for the briefing output."""
    if not reviews:
        return "  No open positions."

    lines: list[str] = []
    action_icons = {
        "CLOSE NOW": "!",
        "TAKE PROFIT": "$",
        "WATCH CLOSELY": "?",
        "HOLD": " ",
    }

    for r in reviews:
        icon = action_icons.get(r.action, " ")
        lines.append(f"  {icon} {r.symbol} — {r.action}")
        lines.append(f"    P&L: ${r.current_pnl:,.0f} | {r.days_to_expiry}d to expiry")
        lines.append(f"    {r.reasoning}")
    return "\n".join(lines)
```

- [ ] **Step 4: Update src/intelligence/__init__.py exports**

```python
# src/intelligence/__init__.py
"""Intelligence mesh — multi-source context assembly."""
from src.intelligence.builder import build_intelligence_context
from src.intelligence.position_review import review_position, format_position_review, PositionReview

__all__ = ["build_intelligence_context", "review_position", "format_position_review", "PositionReview"]
```

- [ ] **Step 5: Wire position review into briefing**

In `src/main.py`, add to the imports:

```python
from src.intelligence.position_review import review_position, format_position_review
from src.data.portfolio import load_portfolio_state
```

In `run_analysis_cycle()`, after building intelligence contexts and before Claude analyst brief, add:

```python
    # 5b. Load portfolio and run position review
    portfolio_state = None
    position_reviews = []
    try:
        portfolio_state = load_portfolio_state()
        if portfolio_state.positions:
            for pos in portfolio_state.positions:
                # Find matching intelligence context
                matching_ctx = next(
                    (c for c in intel_contexts if c.symbol == pos.symbol), None
                )
                if matching_ctx:
                    review = review_position(pos, matching_ctx)
                    position_reviews.append(review)
    except Exception as e:
        log.warning("portfolio_load_skipped", error=str(e))
```

Update `format_local_briefing()` to add `position_reviews` as the final parameter. The complete signature (incorporating Task 7's changes) becomes:

```python
def format_local_briefing(
    regime: RegimeState,
    vix: float,
    spy_change: float,
    all_signals: list[AlphaSignal],
    watchlist_data: list[tuple[str, MarketContext, PriceHistory, OptionsChain, EventCalendar]],
    tax_alerts: list[str],
    recommendations: list[SizedOpportunity] | None = None,
    intel_contexts: list[IntelligenceContext] | None = None,
    analyst_brief: str | None = None,
    position_reviews: list[PositionReview] | None = None,
) -> str:
```

Add the POSITION REVIEW section **before** ACTION PLAN (after the REGIME section):

```python
    # Position review (before action plan — manage existing risk first)
    if position_reviews:
        lines.append(f"\n━━ POSITION REVIEW ━━")
        lines.append(format_position_review(position_reviews))
```

Also add `from src.intelligence.position_review import PositionReview, format_position_review` to the imports in `src/main.py`.

- [ ] **Step 6: Add wiring test for POSITION REVIEW in briefing**

Add to `tests/test_intelligence.py`:

```python
class TestPositionReviewBriefing:
    def test_position_review_renders_in_briefing(self) -> None:
        """format_local_briefing should render POSITION REVIEW when reviews provided."""
        from datetime import datetime
        from src.main import format_local_briefing
        from src.monitor.regime import RegimeState
        from src.intelligence.position_review import PositionReview

        regime = RegimeState(
            regime="hold", vix=19.0, spy_change_pct=0.005,
            severity="normal", target_deployed=0.70, timestamp=datetime.utcnow(),
        )
        reviews = [
            PositionReview(
                symbol="PLTR", action="CLOSE NOW",
                reasoning="Loss stop hit: current $4.50 is 2.5x entry $1.80",
                current_pnl=Decimal("-270"), days_to_expiry=32,
            ),
            PositionReview(
                symbol="NVDA", action="HOLD",
                reasoning="Thesis intact. TradingView: BUY. Trend: uptrend",
                current_pnl=Decimal("340"), days_to_expiry=32,
            ),
        ]

        briefing = format_local_briefing(
            regime=regime,
            vix=19.0,
            spy_change=0.005,
            all_signals=[],
            watchlist_data=[],
            tax_alerts=[],
            position_reviews=reviews,
        )

        assert "POSITION REVIEW" in briefing
        assert "PLTR" in briefing
        assert "CLOSE NOW" in briefing
        assert "HOLD" in briefing
```

- [ ] **Step 7: Run tests**

Run: `uv run pytest tests/test_intelligence.py::TestPositionReview tests/test_intelligence.py::TestPositionReviewBriefing -v`
Expected: All 5 PASS

- [ ] **Step 8: Commit**

```bash
git add src/intelligence/position_review.py src/intelligence/__init__.py src/data/portfolio.py src/main.py tests/test_intelligence.py
git commit -m "feat: add position intelligence with hold/watch/close recommendations"
```

---

## Task 10: Integration Test — Full Pipeline

**Files:**
- Test: `tests/test_intelligence.py` (add integration test)

- [ ] **Step 1: Write integration test**

```python
# Add to tests/test_intelligence.py
class TestIntegrationPipeline:
    def test_full_pipeline_with_mock_data(self) -> None:
        """End-to-end: signals + TV consensus + builder → IntelligenceContext."""
        from src.analysis.signals import detect_all_signals
        from src.intelligence.builder import build_intelligence_context
        from src.delivery.reasoning import build_reasoning_prompt
        from tests.fixtures.market_data import (
            make_market_context, make_price_history,
            make_options_chain, make_event_calendar,
        )
        from tests.fixtures.intelligence import make_technical_consensus

        mkt = make_market_context(price_change_1d=-3.5, iv_rank=70)
        hist = make_price_history(rsi_14=28.0)
        chain = make_options_chain()
        cal = make_event_calendar()

        signals = detect_all_signals("NVDA", mkt, hist, chain, cal)

        tv = make_technical_consensus(
            overall="SELL",
            moving_averages="STRONG_SELL",
            buy_count=4, neutral_count=5, sell_count=17,
        )

        ctx = build_intelligence_context(
            symbol="NVDA",
            signals=signals,
            market=mkt,
            price_history=hist,
            chain=chain,
            calendar=cal,
            technical_consensus=tv,
        )

        assert ctx.symbol == "NVDA"
        assert ctx.quant.signal_count >= 1
        assert ctx.technical_consensus.overall == "SELL"
        assert ctx.quant.trend_direction in ("uptrend", "downtrend", "range")

        # Build reasoning prompt
        prompt = build_reasoning_prompt([ctx])
        assert "NVDA" in prompt
        assert "SELL" in prompt
        assert "QUANT SIGNALS" in prompt

    def test_pipeline_graceful_without_tradingview(self) -> None:
        """Pipeline works when TradingView is None."""
        from src.intelligence.builder import build_intelligence_context
        from src.delivery.reasoning import build_reasoning_prompt
        from tests.fixtures.market_data import (
            make_market_context, make_price_history,
            make_options_chain, make_event_calendar,
        )

        ctx = build_intelligence_context(
            symbol="AAPL",
            signals=[],
            market=make_market_context(),
            price_history=make_price_history(),
            chain=make_options_chain(),
            calendar=make_event_calendar(),
            technical_consensus=None,
        )

        prompt = build_reasoning_prompt([ctx])
        assert "AAPL" in prompt
        assert "TradingView: unavailable" in prompt
```

- [ ] **Step 2: Run integration tests**

Run: `uv run pytest tests/test_intelligence.py::TestIntegrationPipeline -v`
Expected: All PASS

- [ ] **Step 3: Run full test suite**

Run: `uv run pytest tests/ -v --tb=short`
Expected: All tests PASS, no regressions

- [ ] **Step 4: Commit**

```bash
git add tests/test_intelligence.py
git commit -m "test: add integration tests for intelligence mesh pipeline"
```
