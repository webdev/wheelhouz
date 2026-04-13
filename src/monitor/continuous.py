"""Continuous market monitor — tripwire-based intraday detection.

Lightweight polling detects price drops, IV spikes, position risk,
and news events. Only triggers full analysis when a tripwire fires.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal


@dataclass
class TripwireConfig:
    """Thresholds for intraday tripwires."""
    price_drop_threshold: float = 0.025     # 2.5% from open
    sudden_drop_threshold: float = 0.015    # 1.5% since last check
    volume_spike_threshold: float = 3.0     # 3x average
    bounce_from_low_threshold: float = 0.01 # 1% bounce off low
    iv_rank_change_threshold: float = 15.0  # 15-point IV rank change
    iv_rank_cross_50: bool = True
    iv_rank_cross_70: bool = True
    profit_target_pct: float = 0.50         # 50% of max profit
    loss_stop_monthly: float = 2.0          # 2x entry premium
    loss_stop_weekly: float = 1.5           # 1.5x entry premium
    delta_danger: float = 0.50              # |delta| > 0.50
    expiry_risk_dte: int = 2                # DTE <= 2
    expiry_risk_delta: float = 0.30         # with |delta| > 0.30
    max_alerts_per_day: int = 8
    max_alerts_per_hour: int = 3
    max_same_ticker_per_day: int = 2
    reanalysis_cooldown_minutes: int = 15


@dataclass
class TripwireEvent:
    """A detected tripwire firing."""
    event_type: str  # "price_dip", "iv_spike", "profit_target", "loss_stop", etc.
    ticker: str
    severity: str  # "info", "warning", "critical"
    message: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    requires_analysis: bool = True


@dataclass
class MonitorState:
    """Tracks monitor throttling and cooldowns."""
    alerts_today: int = 0
    alerts_this_hour: int = 0
    ticker_alerts_today: dict[str, int] = field(default_factory=dict)
    last_analysis: dict[str, datetime] = field(default_factory=dict)
    last_hour_reset: datetime = field(default_factory=datetime.utcnow)
    last_day_reset: datetime = field(default_factory=datetime.utcnow)

    def reset_if_needed(self) -> None:
        """Reset hourly/daily counters as needed."""
        now = datetime.utcnow()
        if now.date() != self.last_day_reset.date():
            self.alerts_today = 0
            self.ticker_alerts_today = {}
            self.last_day_reset = now
        if now - self.last_hour_reset > timedelta(hours=1):
            self.alerts_this_hour = 0
            self.last_hour_reset = now

    def can_alert(self, ticker: str, config: TripwireConfig) -> bool:
        """Check if we can send another alert for this ticker."""
        self.reset_if_needed()
        if self.alerts_today >= config.max_alerts_per_day:
            return False
        if self.alerts_this_hour >= config.max_alerts_per_hour:
            return False
        ticker_count = self.ticker_alerts_today.get(ticker, 0)
        if ticker_count >= config.max_same_ticker_per_day:
            return False
        return True

    def is_in_cooldown(self, ticker: str, config: TripwireConfig) -> bool:
        """Check if ticker is in reanalysis cooldown."""
        last = self.last_analysis.get(ticker)
        if last is None:
            return False
        elapsed = (datetime.utcnow() - last).total_seconds() / 60
        return elapsed < config.reanalysis_cooldown_minutes

    def record_alert(self, ticker: str) -> None:
        """Record that an alert was sent."""
        self.alerts_today += 1
        self.alerts_this_hour += 1
        self.ticker_alerts_today[ticker] = self.ticker_alerts_today.get(ticker, 0) + 1
        self.last_analysis[ticker] = datetime.utcnow()


def check_price_tripwires(
    ticker: str,
    current_price: Decimal,
    open_price: Decimal,
    prev_check_price: Decimal | None,
    intraday_low: Decimal,
    avg_volume: float,
    current_volume: float,
    config: TripwireConfig | None = None,
) -> list[TripwireEvent]:
    """Check price-based tripwires for a single ticker."""
    cfg = config or TripwireConfig()
    events: list[TripwireEvent] = []

    if open_price <= 0:
        return events

    change_from_open = float((current_price - open_price) / open_price)

    # Intraday dip: down 2.5%+ from open
    if change_from_open <= -cfg.price_drop_threshold:
        events.append(TripwireEvent(
            event_type="price_dip",
            ticker=ticker,
            severity="warning",
            message=f"{ticker} down {change_from_open:.1%} from open",
        ))

    # Sudden drop since last check
    if prev_check_price and prev_check_price > 0:
        sudden = float((current_price - prev_check_price) / prev_check_price)
        if sudden <= -cfg.sudden_drop_threshold:
            events.append(TripwireEvent(
                event_type="sudden_drop",
                ticker=ticker,
                severity="critical",
                message=f"{ticker} dropped {sudden:.1%} since last check",
            ))

    # Volume spike
    if avg_volume > 0 and current_volume / avg_volume >= cfg.volume_spike_threshold:
        events.append(TripwireEvent(
            event_type="volume_spike",
            ticker=ticker,
            severity="info",
            message=(
                f"{ticker} volume {current_volume / avg_volume:.1f}x "
                f"average on down day"
            ),
            requires_analysis=change_from_open < 0,
        ))

    # Bounce from low
    if intraday_low > 0 and open_price > 0:
        drop_from_open = float((intraday_low - open_price) / open_price)
        bounce = float((current_price - intraday_low) / intraday_low)
        if drop_from_open < -0.05 and bounce >= cfg.bounce_from_low_threshold:
            events.append(TripwireEvent(
                event_type="bounce_from_low",
                ticker=ticker,
                severity="info",
                message=(
                    f"{ticker} bounced {bounce:.1%} off intraday low "
                    f"(was down {drop_from_open:.1%})"
                ),
            ))

    return events


def check_iv_tripwires(
    ticker: str,
    current_iv_rank: float,
    morning_iv_rank: float,
    config: TripwireConfig | None = None,
) -> list[TripwireEvent]:
    """Check IV-based tripwires."""
    cfg = config or TripwireConfig()
    events: list[TripwireEvent] = []

    change = current_iv_rank - morning_iv_rank

    # IV rank spiked 15+ points
    if change >= cfg.iv_rank_change_threshold:
        events.append(TripwireEvent(
            event_type="iv_spike",
            ticker=ticker,
            severity="warning",
            message=(
                f"{ticker} IV rank jumped {change:.0f} points "
                f"to {current_iv_rank:.0f}"
            ),
        ))

    # IV rank crossed 50
    if cfg.iv_rank_cross_50 and morning_iv_rank < 50 <= current_iv_rank:
        events.append(TripwireEvent(
            event_type="iv_cross_50",
            ticker=ticker,
            severity="info",
            message=f"{ticker} IV rank crossed 50 (now {current_iv_rank:.0f})",
        ))

    # IV rank crossed 70
    if cfg.iv_rank_cross_70 and morning_iv_rank < 70 <= current_iv_rank:
        events.append(TripwireEvent(
            event_type="iv_cross_70",
            ticker=ticker,
            severity="warning",
            message=f"{ticker} IV rank crossed 70 (now {current_iv_rank:.0f})",
        ))

    return events


def check_position_tripwires(
    ticker: str,
    entry_price: Decimal,
    current_price: Decimal,
    delta: float,
    days_to_expiry: int,
    max_profit: Decimal,
    current_profit: Decimal,
    config: TripwireConfig | None = None,
) -> list[TripwireEvent]:
    """Check position-level tripwires."""
    cfg = config or TripwireConfig()
    events: list[TripwireEvent] = []

    if entry_price <= 0:
        return events

    loss_multiple = float(current_price / entry_price)
    is_weekly = days_to_expiry <= 10
    loss_stop = cfg.loss_stop_weekly if is_weekly else cfg.loss_stop_monthly

    # Profit target hit
    if max_profit > 0 and current_profit >= max_profit * Decimal(str(cfg.profit_target_pct)):
        events.append(TripwireEvent(
            event_type="profit_target",
            ticker=ticker,
            severity="info",
            message=(
                f"{ticker} hit {cfg.profit_target_pct:.0%} profit target "
                f"with {days_to_expiry} DTE"
            ),
        ))

    # Loss stop triggered
    if loss_multiple >= loss_stop:
        events.append(TripwireEvent(
            event_type="loss_stop",
            ticker=ticker,
            severity="critical",
            message=(
                f"{ticker} at {loss_multiple:.1f}x entry — "
                f"loss stop {'(weekly)' if is_weekly else '(monthly)'}"
            ),
        ))
    elif loss_multiple >= 1.7:
        events.append(TripwireEvent(
            event_type="approaching_stop",
            ticker=ticker,
            severity="warning",
            message=f"{ticker} at {loss_multiple:.1f}x entry — approaching loss stop",
        ))

    # Delta danger
    if abs(delta) > cfg.delta_danger:
        events.append(TripwireEvent(
            event_type="delta_danger",
            ticker=ticker,
            severity="warning",
            message=f"{ticker} delta {delta:.2f} — deep ITM risk",
        ))

    # Expiry risk
    if days_to_expiry <= cfg.expiry_risk_dte and abs(delta) > cfg.expiry_risk_delta:
        events.append(TripwireEvent(
            event_type="expiry_risk",
            ticker=ticker,
            severity="critical",
            message=(
                f"{ticker} DTE={days_to_expiry}, delta={delta:.2f} — "
                f"pin/assignment risk"
            ),
        ))

    return events
