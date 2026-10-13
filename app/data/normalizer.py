from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import pandas as pd

from app.core.constants import BAR_MINUTES, JP_TIMEZONE, MARKETS, TIMEFRAME, US_TIMEZONE


def parse_decimal(value: object) -> Decimal | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if text == "" or text.lower() in {"nan", "none", "null"}:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {value}") from exc
    if not number.is_finite():
        raise ValueError(f"not a finite number: {value}")
    return number.quantize(Decimal("0.00000001"))


def parse_timestamp(value: object, source_timezone: str) -> datetime:
    parsed = pd.to_datetime(value, utc=False)
    if pd.isna(parsed):
        raise ValueError(f"invalid timestamp: {value}")
    stamp = pd.Timestamp(parsed)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize(ZoneInfo(source_timezone))
    return stamp.tz_convert("UTC").to_pydatetime()


def market_timezone(market: str) -> ZoneInfo:
    if market == "JP":
        return ZoneInfo(JP_TIMEZONE)
    return ZoneInfo(US_TIMEZONE)


def default_currency(market: str) -> str:
    if market == "JP":
        return "JPY"
    if market == "US":
        return "USD"
    raise ValueError(f"unsupported market: {market}")


def require_market(market: str) -> str:
    normalized = market.strip().upper()
    if normalized not in MARKETS:
        raise ValueError(f"unsupported market: {market}")
    return normalized


def require_timeframe(timeframe: str) -> str:
    if timeframe != TIMEFRAME:
        raise ValueError(f"Phase 1 accepts only {TIMEFRAME}, got {timeframe}")
    return timeframe


def is_bar_closed(timestamp: datetime, now: datetime | None = None) -> bool:
    from app.core.constants import BAR_CLOSE_MARGIN_SECONDS

    current = now or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    close_time = timestamp.astimezone(timezone.utc) + timedelta(minutes=BAR_MINUTES, seconds=BAR_CLOSE_MARGIN_SECONDS)
    return close_time <= current.astimezone(timezone.utc)
