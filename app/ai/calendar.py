from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.core.constants import JP_TIMEZONE, US_TIMEZONE

UTC = ZoneInfo("UTC")
US = ZoneInfo(US_TIMEZONE)
JP = ZoneInfo(JP_TIMEZONE)

# Weekday closures. The list is a maintained calendar, not a live market feed.
US_HOLIDAYS = {
    date(2024, 1, 1),
    date(2024, 1, 15),
    date(2024, 2, 19),
    date(2024, 3, 29),
    date(2024, 5, 27),
    date(2024, 6, 19),
    date(2024, 7, 4),
    date(2024, 9, 2),
    date(2024, 11, 28),
    date(2024, 12, 25),
    date(2025, 1, 1),
    date(2025, 1, 9),
    date(2025, 1, 20),
    date(2025, 2, 17),
    date(2025, 4, 18),
    date(2025, 5, 26),
    date(2025, 6, 19),
    date(2025, 7, 4),
    date(2025, 9, 1),
    date(2025, 11, 27),
    date(2025, 12, 25),
    date(2026, 1, 1),
    date(2026, 1, 19),
    date(2026, 2, 16),
    date(2026, 4, 3),
    date(2026, 5, 25),
    date(2026, 6, 19),
    date(2026, 7, 3),
    date(2026, 9, 7),
    date(2026, 11, 26),
    date(2026, 12, 25),
}
US_EARLY_CLOSES = {
    date(2024, 7, 3),
    date(2024, 11, 29),
    date(2024, 12, 24),
    date(2025, 7, 3),
    date(2025, 11, 28),
    date(2025, 12, 24),
    date(2026, 11, 27),
    date(2026, 12, 24),
}
JP_HOLIDAYS = {
    date(2024, 1, 1),
    date(2024, 1, 2),
    date(2024, 1, 3),
    date(2024, 1, 8),
    date(2024, 2, 12),
    date(2024, 2, 23),
    date(2024, 3, 20),
    date(2024, 4, 29),
    date(2024, 5, 3),
    date(2024, 5, 6),
    date(2024, 7, 15),
    date(2024, 8, 12),
    date(2024, 9, 16),
    date(2024, 9, 23),
    date(2024, 10, 14),
    date(2024, 11, 4),
    date(2024, 11, 23),
    date(2025, 1, 1),
    date(2025, 1, 2),
    date(2025, 1, 3),
    date(2025, 1, 13),
    date(2025, 2, 11),
    date(2025, 2, 24),
    date(2025, 3, 20),
    date(2025, 4, 29),
    date(2025, 5, 5),
    date(2025, 5, 6),
    date(2025, 7, 21),
    date(2025, 8, 11),
    date(2025, 9, 15),
    date(2025, 9, 23),
    date(2025, 10, 13),
    date(2025, 11, 3),
    date(2025, 11, 24),
    date(2026, 1, 1),
    date(2026, 1, 2),
    date(2026, 1, 12),
    date(2026, 2, 11),
    date(2026, 2, 23),
    date(2026, 3, 20),
    date(2026, 4, 29),
    date(2026, 5, 4),
    date(2026, 5, 5),
    date(2026, 5, 6),
    date(2026, 7, 20),
    date(2026, 8, 11),
    date(2026, 9, 21),
    date(2026, 9, 22),
    date(2026, 9, 23),
    date(2026, 10, 12),
    date(2026, 11, 3),
    date(2026, 11, 23),
}


def zone_for(market: str) -> ZoneInfo:
    return JP if market == "JP" else US


def is_trading_day(market: str, day: date) -> bool:
    if day.weekday() >= 5:
        return False
    holidays = JP_HOLIDAYS if market == "JP" else US_HOLIDAYS
    return day not in holidays


def session_close_clock(market: str, day: date) -> time:
    if market == "JP":
        return time(15, 0)
    if day in US_EARLY_CLOSES:
        return time(13, 0)
    return time(16, 0)


def segments(market: str, day: date) -> list[tuple[datetime, datetime]]:
    """Half-open local intervals. Japanese lunch is not inside either segment."""
    if not is_trading_day(market, day):
        return []
    zone = zone_for(market)
    close = session_close_clock(market, day)
    if market == "JP":
        morning_end = min(time(11, 30), close)
        ranges = [(time(9, 0), morning_end)]
        if close > time(12, 30):
            ranges.append((time(12, 30), close))
    else:
        ranges = [(time(9, 30), close)]
    return [
        (datetime.combine(day, start, zone), datetime.combine(day, end, zone))
        for start, end in ranges
        if start < end
    ]


def session_bar_starts(market: str, day: date) -> list[datetime]:
    starts: list[datetime] = []
    for start, end in segments(market, day):
        cursor = start
        while cursor < end:
            starts.append(cursor.astimezone(UTC))
            cursor += timedelta(minutes=5)
    return starts


def segment_id(market: str, timestamp: datetime) -> tuple[date, int] | None:
    local = timestamp.astimezone(zone_for(market))
    for index, (start, end) in enumerate(segments(market, local.date())):
        if start <= local < end:
            return local.date(), index
    return None


def next_trading_days(market: str, day: date, count: int) -> list[date]:
    found: list[date] = []
    cursor = day
    while len(found) < count:
        cursor += timedelta(days=1)
        if is_trading_day(market, cursor):
            found.append(cursor)
    return found


def trading_days_between(market: str, start: date, end: date) -> list[date]:
    days: list[date] = []
    cursor = start
    while cursor <= end:
        if is_trading_day(market, cursor):
            days.append(cursor)
        cursor += timedelta(days=1)
    return days
