"""E*Trade broker client — portfolio, quotes, option chains, transactions.

Converts raw E*Trade API responses into shared models.
Rate-limited: 0.3s between requests (4 req/s market, 2 req/s account).
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog

from src.data.auth import ETradeSession
from src.models.market import OptionsChain
from src.models.position import PortfolioState, Position
from src.models.tax import TaxEngine

logger = structlog.get_logger()

# E*Trade rate limits: 4 req/s market data, 2 req/s account data
_RATE_LIMIT_SLEEP = 0.3
_MAX_QUOTE_BATCH = 25


def _sleep() -> None:
    """Rate-limit pause between API calls."""
    time.sleep(_RATE_LIMIT_SLEEP)


def _parse_date(year: Any, month: Any, day: Any) -> date | None:
    """Parse E*Trade's split date fields into a date object."""
    try:
        y, m, d = int(year), int(month), int(day)
        if y == 0 or m == 0 or d == 0:
            return None
        return date(y, m, d)
    except (ValueError, TypeError):
        return None


def _decimal(value: Any) -> Decimal:
    """Safely convert to Decimal."""
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def _float(value: Any) -> float:
    """Safely convert to float."""
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


# ── Portfolio positions ──────────────────────────────────────────


def fetch_accounts(session: ETradeSession) -> list[dict[str, Any]]:
    """List all E*Trade accounts. Returns raw account dicts."""
    resp = session.accounts.list_accounts()
    accounts = resp["AccountListResponse"]["Accounts"]["Account"]
    logger.info("etrade_accounts_fetched", count=len(accounts))
    return accounts  # type: ignore[no-any-return]


def fetch_portfolio(session: ETradeSession) -> PortfolioState:
    """Pull all positions across all accounts, return a PortfolioState."""
    accounts = fetch_accounts(session)
    all_positions: list[Position] = []
    total_cash = Decimal("0")
    total_nlv = Decimal("0")
    total_buying_power = Decimal("0")

    for account in accounts:
        account_id = account["accountIdKey"]
        _sleep()

        # Get account balance for NLV / buying power
        try:
            balance = session.accounts.get_account_balance(
                account_id, real_time=True, resp_format="json"
            )
            bal_data = balance.get("BalanceResponse", {}).get("Computed", {})
            total_nlv += _decimal(bal_data.get("RealTimeValues", {}).get("totalAccountValue", 0))
            total_cash += _decimal(bal_data.get("cashAvailableForInvestment", 0))
            total_buying_power += _decimal(bal_data.get("RealTimeValues", {}).get("totalAccountValue", 0))
        except Exception:
            logger.warning("etrade_balance_failed", account_id=account_id)

        _sleep()

        # Get positions — retry once on failure (E*Trade sometimes returns 500)
        portfolio = None
        for _attempt in range(2):
            try:
                portfolio = session.accounts.get_account_portfolio(account_id)
                break
            except Exception as e:
                if _attempt == 0:
                    _sleep()
                    continue
                logger.error("etrade_portfolio_failed",
                             account_id=account_id,
                             error=str(e),
                             msg="Account positions missing from briefing — data is incomplete")
        if portfolio is None:
            continue

        if not portfolio or "PortfolioResponse" not in portfolio:
            continue

        # AccountPortfolio can be a list or a single dict depending on
        # sandbox vs production and number of sub-accounts.
        acct_portfolios = portfolio["PortfolioResponse"]["AccountPortfolio"]
        if isinstance(acct_portfolios, dict):
            acct_portfolios = [acct_portfolios]

        for acct_portfolio in acct_portfolios:
            positions_raw = acct_portfolio.get("Position", [])
            if isinstance(positions_raw, dict):
                positions_raw = [positions_raw]
            for raw_pos in positions_raw:
                pos = _parse_position(raw_pos, account_id)
                if pos is not None:
                    all_positions.append(pos)

    state = PortfolioState(
        positions=all_positions,
        cash_available=total_cash,
        buying_power=total_buying_power,
        net_liquidation=total_nlv,
    )
    logger.info(
        "portfolio_fetched",
        positions=len(all_positions),
        nlv=str(total_nlv),
    )
    return state


def _parse_position(raw: dict[str, Any], account_id: str) -> Position | None:
    """Convert a raw E*Trade position dict into a Position model."""
    product = raw.get("Product", {})
    sec_type = product.get("securityType", "")

    symbol = product.get("symbol", raw.get("symbolDescription", ""))
    try:
        quantity = int(float(raw.get("quantity", 0)))
    except (ValueError, TypeError):
        quantity = 0

    # Determine position type
    if sec_type == "OPTN":
        call_put = product.get("callPut", "")
        # Negative quantity = short
        if quantity < 0:
            position_type = "short_call" if call_put == "CALL" else "short_put"
        else:
            position_type = "long_call" if call_put == "CALL" else "long_put"
        option_type = "call" if call_put == "CALL" else "put"
        strike = _decimal(product.get("strikePrice", 0))
        expiration = _parse_date(
            product.get("expiryYear"),
            product.get("expiryMonth"),
            product.get("expiryDay"),
        )
    elif sec_type == "EQ":
        position_type = "long_stock" if quantity > 0 else "short_stock"
        option_type = ""
        strike = Decimal("0")
        expiration = None
    else:
        return None  # skip bonds, mutual funds, etc.

    quick = raw.get("Quick", {})
    current_price = _decimal(quick.get("lastTrade", raw.get("marketValue", 0)))
    cost_basis = _decimal(raw.get("totalCost", 0))
    market_value = _decimal(raw.get("marketValue", 0))

    # Compute derived fields
    days_to_expiry = 0
    if expiration:
        days_to_expiry = max(0, (expiration - date.today()).days)

    pnl = market_value - cost_basis

    return Position(
        symbol=symbol,
        position_type=position_type,
        quantity=abs(quantity),
        strike=strike,
        expiration=expiration,
        entry_price=_decimal(raw.get("pricePaid", 0)),
        current_price=current_price,
        underlying_price=_decimal(quick.get("lastTrade", 0)),
        cost_basis=cost_basis,
        delta=0.0,  # filled in later by market data
        theta=0.0,
        gamma=0.0,
        vega=0.0,
        iv=0.0,
        days_to_expiry=days_to_expiry,
        profit_pct=float(pnl / cost_basis) if cost_basis else 0.0,
        max_profit=_decimal(raw.get("pricePaid", 0)) * 100 if sec_type == "OPTN" else Decimal("0"),
        account_id=account_id,
        engine="",
        option_type=option_type,
        market_value=market_value,
        unrealized_pnl=pnl,
        holding_period_days=0,  # not available from E*Trade directly
    )


# ── Quotes ───────────────────────────────────────────────────────


def fetch_quotes(
    session: ETradeSession,
    symbols: list[str],
) -> dict[str, dict[str, Any]]:
    """Fetch real-time quotes. Batches in groups of 25 per E*Trade limit."""
    all_quotes: dict[str, dict[str, Any]] = {}

    for i in range(0, len(symbols), _MAX_QUOTE_BATCH):
        batch = symbols[i : i + _MAX_QUOTE_BATCH]
        _sleep()
        try:
            resp = session.market.get_quote(batch, detail_flag="ALL")
        except Exception:
            logger.warning("etrade_quotes_failed", symbols=batch)
            continue

        if not resp or "QuoteResponse" not in resp:
            continue

        for q in resp["QuoteResponse"]["QuoteData"]:
            sym = q["Product"]["symbol"]
            all_data = q.get("All", {})
            all_quotes[sym] = {
                "price": _decimal(all_data.get("lastTrade")),
                "change_pct": _float(all_data.get("changeClose")),
                "volume": int(all_data.get("totalVolume", 0)),
                "high_52w": _decimal(all_data.get("high52")),
                "low_52w": _decimal(all_data.get("low52")),
                "pe_ratio": _float(all_data.get("pe")),
                "dividend_yield": _float(all_data.get("dividend")),
                "ex_dividend_date": all_data.get("exDividendDate"),
                "earnings_date": all_data.get("nextEarningDate"),
            }

    logger.info("quotes_fetched", count=len(all_quotes))
    return all_quotes


# ── Option chains ────────────────────────────────────────────────


def fetch_option_chain(
    session: ETradeSession,
    symbol: str,
    expiry_date: date,
    strike_near: float,
    num_strikes: int = 10,
) -> list[dict[str, Any]]:
    """Pull option chain with Greeks from E*Trade.

    expiry_date: exact expiration date (get from get_option_expire_date first).
    """
    _sleep()
    try:
        chain = session.market.get_option_chains(
            symbol,
            expiry_date=expiry_date,
            strike_price_near=int(strike_near),
            no_of_strikes=num_strikes,
            option_category="STANDARD",
            chain_type="CALLPUT",
            price_type="all",
            resp_format="json",
        )
    except Exception as e:
        logger.warning("etrade_chain_failed", symbol=symbol, error=str(e))
        return []

    contracts: list[dict[str, Any]] = []
    if not chain or "OptionChainResponse" not in chain:
        return contracts

    option_pairs = chain["OptionChainResponse"].get("OptionPair", [])
    if isinstance(option_pairs, dict):
        option_pairs = [option_pairs]

    for pair in option_pairs:
        for side in ("Call", "Put"):
            opt = pair.get(side)
            if not opt:
                continue
            greeks = opt.get("OptionGreeks", {})
            contracts.append({
                "symbol": symbol,
                "option_type": side.upper(),
                "strike": _decimal(opt.get("strikePrice")),
                "bid": _decimal(opt.get("bid")),
                "ask": _decimal(opt.get("ask")),
                "last": _decimal(opt.get("lastPrice")),
                "volume": int(opt.get("volume", 0)),
                "open_interest": int(opt.get("openInterest", 0)),
                "in_the_money": opt.get("inTheMoney") == "y",
                "delta": _float(greeks.get("delta")),
                "gamma": _float(greeks.get("gamma")),
                "theta": _float(greeks.get("theta")),
                "vega": _float(greeks.get("vega")),
                "rho": _float(greeks.get("rho")),
                "iv": _float(greeks.get("iv")),
            })

    logger.info("chain_fetched", symbol=symbol, contracts=len(contracts))
    return contracts


def fetch_etrade_chain(
    session: ETradeSession,
    symbol: str,
    current_price: float,
    target_dte: int = 30,
) -> OptionsChain:
    """Fetch full options chain from E*Trade with real bid/ask/Greeks.

    Returns an OptionsChain model (same as yfinance returns) so it's
    a drop-in replacement. Falls back to empty chain on any failure.
    """
    from src.models.market import OptionContract

    # 1. Get available expiration dates
    _sleep()
    try:
        exp_resp = session.market.get_option_expire_date(symbol)
    except Exception:
        logger.warning("etrade_expirations_failed", symbol=symbol)
        return OptionsChain(symbol=symbol)

    if not exp_resp or "OptionExpireDateResponse" not in exp_resp:
        return OptionsChain(symbol=symbol)

    raw_exps = exp_resp["OptionExpireDateResponse"].get("ExpirationDate", [])
    if isinstance(raw_exps, dict):
        raw_exps = [raw_exps]

    exp_dates: list[date] = []
    for exp in raw_exps:
        d = _parse_date(exp.get("year"), exp.get("month"), exp.get("day"))
        if d:
            exp_dates.append(d)

    if not exp_dates:
        return OptionsChain(symbol=symbol)

    # Pick the expiration closest to target DTE
    today = date.today()
    target_date = today + timedelta(days=target_dte)
    best_exp = min(exp_dates, key=lambda d: abs((d - target_date).days))

    # 2. Fetch the chain for that expiration
    raw_contracts = fetch_option_chain(
        session=session,
        symbol=symbol,
        expiry_date=best_exp,
        strike_near=current_price,
        num_strikes=20,
    )

    if not raw_contracts:
        return OptionsChain(symbol=symbol, expirations=exp_dates)

    # 3. Convert to OptionContract models
    puts: list[OptionContract] = []
    calls: list[OptionContract] = []

    for c in raw_contracts:
        bid = c["bid"]
        ask = c["ask"]
        mid = (bid + ask) / 2

        contract = OptionContract(
            strike=c["strike"],
            expiration=best_exp,
            option_type=c["option_type"].lower(),
            bid=bid,
            ask=ask,
            mid=Decimal(str(round(float(mid), 2))),
            volume=c["volume"],
            open_interest=c["open_interest"],
            implied_vol=c["iv"],
            delta=c["delta"],
        )

        if c["option_type"] == "PUT":
            puts.append(contract)
        else:
            calls.append(contract)

    # ATM IV from nearest-ATM put
    atm_iv = None
    if puts:
        atm_put = min(puts, key=lambda p: abs(float(p.strike) - current_price))
        atm_iv = atm_put.implied_vol

    logger.info("etrade_chain_built", symbol=symbol, puts=len(puts), calls=len(calls))
    return OptionsChain(
        symbol=symbol,
        puts=puts,
        calls=calls,
        atm_iv=atm_iv,
        expirations=exp_dates,
    )


# ── YTD Orders (for realized P&L) ──────────────────────────────


def fetch_ytd_option_orders(
    session: ETradeSession,
) -> list[dict[str, Any]]:
    """Fetch all executed option orders YTD across all accounts.

    Uses the Orders API (list_orders with status=EXECUTED) instead of
    the Transactions API which requires separate authorization.
    Paginates through all accounts, max 100 orders per page.
    """
    accounts = fetch_accounts(session)
    jan1 = datetime(date.today().year, 1, 1)
    today = datetime.combine(date.today(), datetime.min.time())
    all_orders: list[dict[str, Any]] = []

    for account in accounts:
        account_id = account["accountIdKey"]
        marker: str | None = None

        while True:
            _sleep()
            try:
                resp = session.order.list_orders(
                    account_id,
                    status="EXECUTED",
                    from_date=jan1,
                    to_date=today,
                    security_type="OPTN",
                    count=100,
                    marker=marker,
                    resp_format="json",
                )
            except Exception as e:
                logger.warning("etrade_orders_failed",
                               account_id=account_id, error=str(e))
                break

            if not resp or "OrdersResponse" not in resp:
                break

            order_list = resp["OrdersResponse"].get("Order", [])
            if isinstance(order_list, dict):
                order_list = [order_list]

            if not order_list:
                break

            for order in order_list:
                order["_account_id"] = account_id
            all_orders.extend(order_list)

            # Pagination
            marker = resp["OrdersResponse"].get("marker")
            if not marker:
                break

    logger.info("ytd_option_orders_fetched", count=len(all_orders))
    return all_orders


def populate_tax_engine_from_orders(
    orders: list[dict[str, Any]],
) -> TaxEngine:
    """Parse executed E*Trade option orders into YTD P&L.

    Each order has OrderDetail → Instrument[] with:
    - orderAction: BUY_OPEN, SELL_OPEN, BUY_CLOSE, SELL_CLOSE
    - filledQuantity, averageExecutionPrice
    - Product: symbol, securityType, callPut, strikePrice, expiryYear/Month/Day

    SELL_OPEN = premium received (new short position)
    BUY_CLOSE = premium paid to close (match against opens)
    """
    engine = TaxEngine()
    # Track opens by option key for matching
    # Key: (account_id, symbol, strike, expiry, callput) → list of (premium_per_contract, date)
    open_premiums: dict[tuple, list[tuple[Decimal, date]]] = {}

    for order in orders:
        acct = order.get("_account_id", "")
        details = order.get("OrderDetail", [])
        if isinstance(details, dict):
            details = [details]

        for detail in details:
            instruments = detail.get("Instrument", [])
            if isinstance(instruments, dict):
                instruments = [instruments]

            exec_date = _parse_order_date(detail.get("executedTime"))

            for inst in instruments:
                product = inst.get("Product", {})
                if product.get("securityType") != "OPTN":
                    continue

                action = inst.get("orderAction", "")
                filled_qty = int(float(inst.get("filledQuantity", 0) or 0))
                avg_price = _decimal(inst.get("averageExecutionPrice", 0))

                if filled_qty <= 0:
                    continue

                # Build matching key from option contract details
                option_key = (
                    acct,
                    product.get("symbol", ""),
                    str(product.get("strikePrice", "")),
                    f"{product.get('expiryYear', '')}-{product.get('expiryMonth', '')}-{product.get('expiryDay', '')}",
                    product.get("callPut", ""),
                )

                premium_total = avg_price * filled_qty * 100

                if action == "SELL_OPEN":
                    engine.option_premium_income_ytd += premium_total
                    open_premiums.setdefault(option_key, []).append(
                        (avg_price, exec_date or date.today())
                    )

                elif action == "BUY_CLOSE":
                    close_cost = premium_total
                    opens = open_premiums.get(option_key, [])
                    if opens:
                        open_price, open_date = opens.pop(0)
                        pnl = (open_price - avg_price) * filled_qty * 100
                    else:
                        # No matching open in YTD — likely opened last year.
                        # Can't compute exact P&L, skip rather than guess.
                        continue

                    if pnl >= 0:
                        engine.realized_stcg_ytd += pnl
                    else:
                        engine.realized_losses_ytd += abs(pnl)
                        symbol = product.get("symbol", "")
                        if symbol and exec_date:
                            engine.wash_sale_tracker.record_loss(
                                symbol, exec_date, abs(pnl)
                            )

                elif action in ("SELL_CLOSE", "BUY_OPEN"):
                    # SELL_CLOSE = closing a long option (not wheel strategy)
                    # BUY_OPEN = buying to open (not premium selling)
                    # Track but don't count as wheel premium income
                    pass

    logger.info(
        "tax_engine_populated",
        premium_income=str(engine.option_premium_income_ytd),
        realized_stcg=str(engine.realized_stcg_ytd),
        realized_losses=str(engine.realized_losses_ytd),
    )
    return engine


def _parse_order_date(date_val: Any) -> date | None:
    """Parse E*Trade order execution date (epoch ms or string)."""
    if date_val is None:
        return None
    try:
        if isinstance(date_val, (int, float)):
            return datetime.fromtimestamp(date_val / 1000).date()
        return datetime.fromisoformat(str(date_val)).date()
    except (ValueError, TypeError, OSError):
        return None
