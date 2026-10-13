from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.data.bar import NormalizedBar
from app.data.csv_loader import load_csv
from app.data.normalizer import require_timeframe
from app.data.providers.base import MarketDataProvider


class CsvProvider(MarketDataProvider):
    name = "csv"

    def __init__(self, path: Path, source_timezone: str = "UTC") -> None:
        self.path = path
        self.source_timezone = source_timezone

    async def get_bars(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> list[NormalizedBar]:
        require_timeframe(timeframe)
        loaded = load_csv(self.path, self.source_timezone, provider=self.name)
        if loaded.errors and not loaded.bars:
            raise ValueError("; ".join(loaded.errors))
        symbol = symbol.upper()
        start_utc = start.astimezone(timezone.utc)
        end_utc = end.astimezone(timezone.utc)
        return [
            bar
            for bar in loaded.bars
            if bar.symbol == symbol and bar.timeframe == timeframe and start_utc <= bar.timestamp < end_utc
        ]

    async def get_latest_bar(self, symbol: str, timeframe: str) -> NormalizedBar | None:
        require_timeframe(timeframe)
        loaded = load_csv(self.path, self.source_timezone, provider=self.name)
        symbol = symbol.upper()
        matches = [bar for bar in loaded.bars if bar.symbol == symbol and bar.timeframe == timeframe]
        if not matches:
            return None
        return max(matches, key=lambda bar: bar.timestamp)
