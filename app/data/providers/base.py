from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from app.data.bar import NormalizedBar


class MarketDataProvider(ABC):
    name: str

    @abstractmethod
    async def get_bars(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> list[NormalizedBar]:
        """Return normalized bars in [start, end). Do not fill missing timestamps."""

    @abstractmethod
    async def get_latest_bar(self, symbol: str, timeframe: str) -> NormalizedBar | None:
        """Return the newest closed bar, or None when the source has no bar."""


class ProviderError(RuntimeError):
    pass
