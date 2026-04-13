"""Shared enumerations used across all modules."""

from enum import Enum


class SignalType(Enum):
    """Alpha signal types for opportunity detection."""
    # Dip detection
    INTRADAY_DIP = "intraday_dip"
    MULTI_DAY_PULLBACK = "multi_day_pullback"
    SECTOR_ROTATION = "sector_rotation"
    EARNINGS_OVERREACTION = "earnings_overreaction"
    MACRO_FEAR_SPIKE = "macro_fear_spike"

    # IV surface dislocations
    IV_RANK_SPIKE = "iv_rank_spike"
    SKEW_BLOW_OUT = "skew_blowout"
    TERM_STRUCTURE_INVERSION = "term_inversion"
    IV_CRUSH_SETUP = "iv_crush_setup"

    # Technical levels
    SUPPORT_BOUNCE = "support_bounce"
    OVERSOLD_RSI = "oversold_rsi"
    VOLUME_CLIMAX = "volume_climax"
    GAP_FILL = "gap_fill"

    # Flow / sentiment
    UNUSUAL_PUT_SELLING = "unusual_put_selling"
    DARK_POOL_ACCUMULATION = "dark_pool"
    SHORT_INTEREST_SQUEEZE = "short_squeeze"


class PositionType(Enum):
    """Types of portfolio positions."""
    SHORT_PUT = "short_put"
    SHORT_CALL = "short_call"
    LONG_STOCK = "long_stock"
    LONG_PUT = "long_put"
    LONG_CALL = "long_call"
    CASH = "cash"


class PositionAction(Enum):
    """Actions the scanner can recommend for existing positions."""
    LET_EXPIRE = "let_expire"
    CLOSE_EARLY = "close_early"
    CLOSE_AND_RELOAD = "close_reload"
    ROLL_OUT = "roll_out"
    ROLL_OUT_AND_UP = "roll_out_up"
    ROLL_OUT_AND_DOWN = "roll_out_down"
    ROLL_DOWN_AGGRESSIVE = "roll_down"
    TAKE_ASSIGNMENT = "take_assignment"
    DOUBLE_DOWN = "double_down"
    ALERT_EARNINGS = "alert_earnings"
    ALERT_DIVIDEND = "alert_dividend"
    MONITOR = "monitor"


class Conviction(Enum):
    """Trade conviction levels for position sizing."""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Regime(Enum):
    """Market regime based on VIX thresholds."""
    ATTACK = "attack"   # VIX < 18: sell premium aggressively
    HOLD = "hold"       # VIX 18-25: normal operations
    DEFEND = "defend"   # VIX 25-35: reduce exposure
    CRISIS = "crisis"   # VIX > 35: close weeklies, block new trades


class Engine(Enum):
    """Portfolio engine assignment."""
    ENGINE1 = "engine1"  # Core holdings (45%)
    ENGINE2 = "engine2"  # Active wheel (45%)
    ENGINE3 = "engine3"  # Dry powder (10%)


class AccountType(Enum):
    """Brokerage account types."""
    TAXABLE = "taxable"
    ROTH_IRA = "roth_ira"
    TRADITIONAL_IRA = "traditional_ira"
    ROLLOVER_401K = "401k_rollover"


class EntryDecision(Enum):
    """How to enter a position."""
    BUY_SHARES = "buy_shares"
    SELL_PUT = "sell_put"
    SPLIT_ENTRY = "split_entry"
    SELL_PUT_TARGETING_ASSIGNMENT = "put_to_own"


class Strategy(Enum):
    """The 6 wheel strategy types."""
    MONTHLY_PUT = "monthly_put"
    WEEKLY_PUT = "weekly_put"
    STRANGLE = "strangle"
    EARNINGS_CRUSH = "earnings_crush"
    PUT_SPREAD = "put_spread"
    DIVIDEND_CAPTURE = "dividend_capture"


class Urgency(Enum):
    """Transition action urgency levels."""
    IMMEDIATE = "immediate"
    SHORT_TERM = "short_term"
    MEDIUM_TERM = "medium_term"
