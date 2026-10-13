from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.core.constants import BAR_MINUTES, TIMEFRAME
from app.data.bar import NormalizedBar
from app.data.normalizer import require_timeframe
from app.data.providers.base import MarketDataProvider


class MockProvider(MarketDataProvider):
    """Deterministic bars for tests. Never selected unless MARKET_DATA_PROVIDER=mock."""

    name = "mock"

    def __init__(self, market: str = "US") -> None:
        self.market = market

    async def get_bars(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> list[NormalizedBar]:
        require_timeframe(timeframe)
        cursor = _align(start)
        finish = end.astimezone(timezone.utc)
        bars: list[NormalizedBar] = []
        price = Decimal("100")
        while cursor < finish and len(bars) < 30:
            bars.append(
                NormalizedBar(
                    market=self.market,
                    symbol=symbol.upper(),
                    timestamp=cursor,
                    timeframe=TIMEFRAME,
                    open=price,
                    high=price + Decimal("0.50"),
                    low=price - Decimal("0.40"),
                    close=price + Decimal("0.10"),
                    volume=Decimal("1000"),
                    provider=self.name,
                )
            )
            cursor += timedelta(minutes=BAR_MINUTES)
        return bars

    async def get_latest_bar(self, symbol: str, timeframe: str) -> NormalizedBar | None:
        end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        start = end - timedelta(minutes=BAR_MINUTES * 3)
        bars = await self.get_bars(symbol, timeframe, start, end)
        closed = [bar for bar in bars if bar.timestamp + timedelta(minutes=BAR_MINUTES) <= end]
        return closed[-1] if closed else None


def _align(value: datetime) -> datetime:
    stamp = value.astimezone(timezone.utc).replace(second=0, microsecond=0)
    stamp = stamp.replace(minute=stamp.minute - (stamp.minute % BAR_MINUTES))
    return stamp
