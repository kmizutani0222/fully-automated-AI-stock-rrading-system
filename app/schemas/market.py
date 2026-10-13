from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class BarOut(BaseModel):
    market: str
    symbol: str
    timeframe: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    vwap: float | None
    trade_count: int | None
    provider: str


class GapOut(BaseModel):
    symbol: str
    previous: datetime
    current: datetime
    minutes: float


class QualityOut(BaseModel):
    symbol: str
    rows: int
    gap_count: int
    gaps: list[GapOut]


class ImportOut(BaseModel):
    saved: int
    rejected: int
    duplicate_keys: int
    gap_count: int
    dropped_open_bars: int
    errors: list[str]
