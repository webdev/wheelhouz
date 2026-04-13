"""Earnings, dividends, and macro event calendar.

Pulls event data from yfinance and static Fed calendar.
Builds EventCalendar models for the analysis engine.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import structlog
import yfinance as yf

from src.models.market import EventCalendar

logger = structlog.get_logger()

# Assumption: Fed meetings are published well in advance. This is a static
# list for 2026 — update annually or scrape from federalreserve.gov.
FED_MEETINGS_2026 = [
    date(2026, 1, 28),
    date(2026, 3, 18),
    date(2026, 5, 6),
    date(2026, 6, 17),
    date(2026, 7, 29),
    date(2026, 9, 16),
    date(2026, 10, 28),
    date(2026, 12, 9),
]


def _next_fed_meeting() -> date | None:
    """Return the next upcoming Fed meeting date."""
    today = date.today()
    for d in FED_MEETINGS_2026:
        if d >= today:
            return d
    return None


def fetch_event_calendar(symbol: str) -> EventCalendar:
    """Build an EventCalendar for a symbol from yfinance."""
    ticker = yf.Ticker(symbol)

    # Earnings date
    next_earnings: date | None = None
    earnings_confirmed = False
    try:
        cal = ticker.calendar
        if cal is not None:
            # yfinance returns different formats depending on version
            if isinstance(cal, dict):
                raw = cal.get("Earnings Date")
                if raw:
                    if isinstance(raw, list) and len(raw) > 0:
                        next_earnings = _to_date(raw[0])
                    else:
                        next_earnings = _to_date(raw)
            # Some versions return a DataFrame
            elif hasattr(cal, "loc"):
                try:
                    raw = cal.loc["Earnings Date"]
                    if hasattr(raw, "iloc"):
                        next_earnings = _to_date(raw.iloc[0])
                    else:
                        next_earnings = _to_date(raw)
                except (KeyError, IndexError):
                    pass
    except Exception:
        logger.debug("earnings_fetch_failed", symbol=symbol)

    # Dividend date and amount
    next_ex_div: date | None = None
    div_amount: Decimal | None = None
    try:
        divs = ticker.dividends
        if divs is not None and not divs.empty:
            last_div_date = divs.index[-1]
            last_amount = float(divs.iloc[-1])
            div_amount = Decimal(str(round(last_amount, 4)))

            # Estimate next ex-div: assume quarterly from last
            if hasattr(last_div_date, "date"):
                last_d = last_div_date.date()
            else:
                last_d = last_div_date
            estimated_next = last_d + timedelta(days=90)
            if estimated_next >= date.today():
                next_ex_div = estimated_next
    except Exception:
        logger.debug("dividend_fetch_failed", symbol=symbol)

    return EventCalendar(
        symbol=symbol,
        next_earnings=next_earnings,
        earnings_confirmed=earnings_confirmed,
        next_ex_dividend=next_ex_div,
        dividend_amount=div_amount,
        fed_meeting=_next_fed_meeting(),
    )


def _to_date(value: object) -> date | None:
    """Convert various datetime-like objects to date."""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if hasattr(value, "date"):
        result = value.date()  # type: ignore[union-attr]
        if isinstance(result, date):
            return result
        return None
    try:
        # String like "2026-05-15"
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None
