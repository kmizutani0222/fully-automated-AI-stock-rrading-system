from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.ai.calendar import is_trading_day, session_bar_starts, zone_for
from app.core.constants import BAR_MINUTES

FILL_MODEL = (
    "バックテストは次足始値にスプレッドとスリッページを加えた価格で約定する。"
    "リアルタイムのペーパー取引は、注文送信より後に観測した価格だけを使う。"
    "bid/askがある買いはask、売りはbidを使う。"
    "板が無い場合は、注文後に確定した終値へ設定済みのスプレッドとスリッページを加える。"
    "注文後の価格が無い注文は未約定のまま残す。未確定足は使わない。"
)


def bar_ready(bar_start: datetime, now: datetime, delay_seconds: int) -> bool:
    """A 5-minute bar is usable only after it has closed and the arrival delay has passed."""
    start = _aware(bar_start)
    current = _aware(now)
    return current >= start + timedelta(minutes=BAR_MINUTES, seconds=delay_seconds)


def due_bars(market: str, now: datetime, delay_seconds: int, *, lookback_days: int = 4) -> list[datetime]:
    current = _aware(now).astimezone(zone_for(market))
    found: list[datetime] = []
    for offset in range(lookback_days, -1, -1):
        day = current.date() - timedelta(days=offset)
        if not is_trading_day(market, day):
            continue
        for start in session_bar_starts(market, day):
            if bar_ready(start, now, delay_seconds):
                found.append(start)
    return found


def market_day(market: str, now: datetime) -> date:
    return _aware(now).astimezone(zone_for(market)).date()


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
