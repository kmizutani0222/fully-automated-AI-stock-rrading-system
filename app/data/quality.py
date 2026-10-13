from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.core.constants import BAR_MINUTES
from app.data.bar import NormalizedBar, RejectedBar
from app.data.normalizer import is_bar_closed, market_timezone
from app.data.validator import validate_bar


@dataclass
class Gap:
    market: str
    symbol: str
    previous: datetime
    current: datetime
    minutes: float


@dataclass
class QualityReport:
    rows: int = 0
    valid_rows: int = 0
    rejected: list[RejectedBar] = field(default_factory=list)
    duplicate_keys: list[str] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    dropped_open_bars: int = 0


def bar_key(bar: NormalizedBar) -> str:
    stamp = bar.timestamp.isoformat()
    return f"{bar.market}:{bar.symbol}:{bar.timeframe}:{stamp}:{bar.provider}"


def partition_bars(bars: list[NormalizedBar]) -> tuple[list[NormalizedBar], list[RejectedBar]]:
    valid: list[NormalizedBar] = []
    rejected: list[RejectedBar] = []
    for bar in bars:
        reasons = validate_bar(bar)
        if reasons:
            rejected.append(RejectedBar(symbol=bar.symbol, timestamp=bar.timestamp, reasons=tuple(reasons)))
        else:
            valid.append(bar)
    return valid, rejected


def drop_open_bars(bars: list[NormalizedBar], now: datetime | None = None) -> tuple[list[NormalizedBar], int]:
    closed = [bar for bar in bars if is_bar_closed(bar.timestamp, now)]
    return closed, len(bars) - len(closed)


def _is_expected_break(previous: NormalizedBar, current: NormalizedBar) -> bool:
    if previous.market != "JP" or current.market != "JP":
        return False
    zone = market_timezone("JP")
    before = previous.timestamp.astimezone(zone)
    after = current.timestamp.astimezone(zone)
    return (
        before.hour == 11
        and before.minute == 30
        and after.hour == 12
        and after.minute == 30
        and before.date() == after.date()
    )


def inspect_bars(bars: list[NormalizedBar]) -> QualityReport:
    """Report invalid rows, duplicate keys, and intraday gaps. Missing bars are not created."""
    report = QualityReport(rows=len(bars))
    valid, rejected = partition_bars(bars)
    report.rejected = rejected
    seen: dict[str, NormalizedBar] = {}
    for bar in valid:
        key = bar_key(bar)
        if key in seen:
            report.duplicate_keys.append(key)
        seen[key] = bar
    unique = sorted(seen.values(), key=lambda bar: (bar.market, bar.symbol, bar.provider, bar.timestamp))
    report.valid_rows = len(unique)
    for previous, current in zip(unique, unique[1:], strict=False):
        if (previous.market, previous.symbol, previous.provider) != (current.market, current.symbol, current.provider):
            continue
        zone = market_timezone(previous.market)
        if previous.timestamp.astimezone(zone).date() != current.timestamp.astimezone(zone).date():
            continue
        delta = current.timestamp - previous.timestamp
        if delta <= timedelta(minutes=BAR_MINUTES):
            continue
        if _is_expected_break(previous, current):
            continue
        report.gaps.append(
            Gap(
                market=previous.market,
                symbol=previous.symbol,
                previous=previous.timestamp,
                current=current.timestamp,
                minutes=delta.total_seconds() / 60,
            )
        )
    return report
