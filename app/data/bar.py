from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class NormalizedBar(BaseModel):
    model_config = ConfigDict(frozen=True)

    market: str
    symbol: str
    timestamp: datetime
    timeframe: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None = None
    vwap: Decimal | None = None
    trade_count: int | None = None
    provider: str
    is_adjusted: bool = False


class RejectedBar(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    timestamp: datetime | None
    reasons: tuple[str, ...] = Field(default_factory=tuple)
