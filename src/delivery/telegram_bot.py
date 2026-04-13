"""Telegram bot — message delivery, formatting, alerting.

Handles splitting long messages, inline keyboard for section drill-down,
live-price gate alerts, and alert throttling.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal
from enum import Enum

from src.models.execution import LivePriceGate
from src.models.market import MarketContext
from src.models.signals import AlphaSignal


TELEGRAM_MAX_LEN = 4096


# ── Alert priority & throttling ────────────────────────────────


class AlertPriority(Enum):
    CRITICAL = "critical"    # immediate: loss stops, expiry risk
    HIGH = "high"            # push within 1 min: 2+ signals
    MEDIUM = "medium"        # batch every 15 min: profit targets
    LOW = "low"              # include in post-market review only


@dataclass
class AlertThrottling:
    max_alerts_per_day: int = 8
    max_alerts_per_hour: int = 3
    max_same_ticker_per_day: int = 2
    quiet_start: str = "11:30"
    quiet_end: str = "13:00"
    critical_ignores_throttle: bool = True
    post_trade_cooldown_minutes: int = 30

    # Runtime tracking (reset daily)
    _alerts_today: int = 0
    _alerts_this_hour: int = 0
    _ticker_counts: dict[str, int] = field(default_factory=dict)
    _last_alert_time: datetime | None = None

    def should_throttle(
        self, symbol: str, priority: AlertPriority, now: datetime | None = None,
    ) -> tuple[bool, str]:
        """Return (throttled, reason). Critical alerts bypass throttling."""
        if priority == AlertPriority.CRITICAL and self.critical_ignores_throttle:
            return (False, "")

        now = now or datetime.utcnow()

        if self._alerts_today >= self.max_alerts_per_day:
            return (True, f"Daily limit ({self.max_alerts_per_day}) reached")

        if self._alerts_this_hour >= self.max_alerts_per_hour:
            return (True, f"Hourly limit ({self.max_alerts_per_hour}) reached")

        ticker_count = self._ticker_counts.get(symbol, 0)
        if ticker_count >= self.max_same_ticker_per_day:
            return (True, f"{symbol} limit ({self.max_same_ticker_per_day}/day) reached")

        # Quiet hours (ET)
        qs_parts = [int(x) for x in self.quiet_start.split(":")]
        qe_parts = [int(x) for x in self.quiet_end.split(":")]
        quiet_s = time(qs_parts[0], qs_parts[1])
        quiet_e = time(qe_parts[0], qe_parts[1])
        if quiet_s <= now.time() <= quiet_e:
            return (True, "Quiet hours")

        # Cooldown after last alert
        if self._last_alert_time:
            cooldown = timedelta(minutes=self.post_trade_cooldown_minutes)
            if now - self._last_alert_time < cooldown:
                return (True, "Post-trade cooldown")

        return (False, "")

    def record_alert(self, symbol: str) -> None:
        """Update counters after sending an alert."""
        self._alerts_today += 1
        self._alerts_this_hour += 1
        self._ticker_counts[symbol] = self._ticker_counts.get(symbol, 0) + 1
        self._last_alert_time = datetime.utcnow()

    def reset_daily(self) -> None:
        self._alerts_today = 0
        self._alerts_this_hour = 0
        self._ticker_counts.clear()
        self._last_alert_time = None

    def reset_hourly(self) -> None:
        self._alerts_this_hour = 0


# ── Message splitting ───────────────────────────────────────────


def split_message(text: str, max_len: int = TELEGRAM_MAX_LEN) -> list[str]:
    """Split text into chunks that fit Telegram's message limit.

    Tries to split on double-newlines, then single newlines, then hard-cuts.
    """
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break

        # Try to split at a section boundary
        cut = remaining[:max_len]
        split_at = cut.rfind("\n\n")
        if split_at == -1 or split_at < max_len // 2:
            split_at = cut.rfind("\n")
        if split_at == -1 or split_at < max_len // 2:
            split_at = max_len

        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()

    return chunks


# ── Telegram send ───────────────────────────────────────────────


async def send_telegram(text: str, parse_mode: str = "Markdown") -> None:
    """Send a single message via Telegram Bot API."""
    import telegram  # type: ignore[import-not-found]

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")

    bot = telegram.Bot(token=token)
    await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)


async def send_briefing(briefing: str) -> None:
    """Send a full briefing, splitting across messages if needed."""
    for chunk in split_message(briefing):
        await send_telegram(chunk)


# ── TelegramFormatter ──────────────────────────────────────────


class TelegramFormatter:
    """Format briefings for mobile: 3-line summary + expandable sections."""

    # Section headers used in briefing text
    SECTIONS = {
        "full_briefing": None,  # entire briefing
        "trades_only": "ATTACK PLAN",
        "pnl_only": "PORTFOLIO SCORECARD",
        "alerts": ["GUARDRAILS", "TAX ALERTS"],
        "tax": ["TAX ALERTS", "ACCOUNTS"],
        "positions": "POSITION MANAGEMENT",
    }

    def format_morning_briefing(self, briefing: str) -> dict[str, object]:
        """Split briefing into a summary + section dict for drill-down."""
        summary = self.extract_summary(briefing)
        sections: dict[str, str] = {"full_briefing": briefing}

        for key, header in self.SECTIONS.items():
            if key == "full_briefing":
                continue
            if isinstance(header, list):
                sections[key] = "\n\n".join(
                    self.extract_section(briefing, h) for h in header
                )
            elif isinstance(header, str):
                sections[key] = self.extract_section(briefing, header)

        return {"summary": summary, "sections": sections}

    def extract_summary(self, briefing: str) -> str:
        """3-line summary that fits one phone screen."""
        regime = self.extract_value(briefing, "REGIME") or "HOLD"
        theta = self.extract_value(briefing, "Daily theta") or "$0/day"
        ytd = self.extract_value(briefing, "YTD") or "N/A"
        num_trades = self.count_trades(briefing)
        num_alerts = self.count_alerts(briefing)

        line1 = f"{regime} | {theta} theta | YTD {ytd}"
        line2 = f"{num_trades} trade(s) proposed"
        line3 = f"{num_alerts} alert(s)" if num_alerts else "No alerts"
        return f"{line1}\n{line2}\n{line3}"

    def extract_section(self, briefing: str, header: str) -> str:
        """Extract content between a section header and the next one."""
        # Match "== HEADER ==" or "-- HEADER --" style
        pattern = rf"[━=\-]{{2,}}\s*{re.escape(header)}\s*[━=\-]{{0,}}"
        match = re.search(pattern, briefing)
        if not match:
            return ""

        start = match.end()
        # Find next section header
        next_header = re.search(r"[━=\-]{2,}\s*[A-Z]", briefing[start:])
        end = start + next_header.start() if next_header else len(briefing)
        return briefing[start:end].strip()

    def extract_value(self, briefing: str, key: str) -> str | None:
        """Extract a named value from the briefing text."""
        pattern = rf"{re.escape(key)}[:\s]+([^\n]+)"
        match = re.search(pattern, briefing, re.IGNORECASE)
        return match.group(1).strip() if match else None

    def count_trades(self, briefing: str) -> int:
        """Count trade proposals in the ATTACK PLAN section."""
        attack = self.extract_section(briefing, "ATTACK PLAN")
        if not attack:
            return 0
        # Count lines that look like "SELL ... P/C" or numbered trades
        trade_lines = re.findall(r"(?:SELL|BUY)\s+\d+x", attack, re.IGNORECASE)
        if trade_lines:
            return len(trade_lines)
        # Fallback: count numbered items
        return len(re.findall(r"^\d+\.", attack, re.MULTILINE))

    def count_alerts(self, briefing: str) -> int:
        """Count alerts across guardrails and tax sections."""
        count = 0
        for section in ["GUARDRAILS", "TAX ALERTS"]:
            text = self.extract_section(briefing, section)
            if text:
                # Count warning emoji lines or bullet points
                count += len(re.findall(r"[^\n]*", text))
        return count


# ── Trade alert formatting ──────────────────────────────────────


def format_gated_alert(gate: LivePriceGate, mkt: MarketContext) -> str:
    """Format a live-price gate alert for Telegram."""
    signals_str = ", ".join(s.signal_type.value for s in gate.signals[:3])
    return (
        f"TRADE ALERT: {gate.symbol} | {gate.conviction.upper()} conviction\n\n"
        f"SELL {gate.symbol} {gate.strike}P {gate.expiration}\n"
        f"Premium: ${gate.analysis_premium:.2f}\n\n"
        f"VALID WHILE:\n"
        f"  {gate.symbol} ${gate.underlying_floor:.2f}"
        f" - ${gate.underlying_ceiling:.2f}"
        f"  (now ${mkt.price:.2f})\n"
        f"  Premium >= ${gate.min_premium:.2f}\n"
        f"  IV rank >= {gate.min_iv_rank:.0f}  (now {mkt.iv_rank:.0f})\n\n"
        f"SIGNALS: {signals_str}\n\n"
        f"Tap EXECUTE — system validates live price first.\n\n"
        f"[EXECUTE]  [SKIP]"
    )


def format_execution_result(
    gate: LivePriceGate,
    valid: bool,
    reason: str,
    live_data: dict[str, object],
) -> str:
    """Format the result of an EXECUTE tap."""
    if valid:
        return (
            f"EXECUTED: {gate.symbol} {gate.strike}P\n"
            f"Limit order placed at ${float(str(live_data.get('premium', 0))) - 0.01:.2f}\n"
            f"Underlying: ${live_data.get('price', 0)}\n"
            f"IV rank: {live_data.get('iv_rank', 0)}\n"
            f"All gate conditions passed. Order is live."
        )
    return (
        f"BLOCKED: {gate.symbol} {gate.strike}P\n"
        f"{reason}\n"
        f"Current: ${live_data.get('price', 0)} | "
        f"Premium: ${live_data.get('premium', 0)}\n"
        f"System protected you from a stale trade.\n"
        f"Will re-evaluate in the next analysis cycle."
    )
