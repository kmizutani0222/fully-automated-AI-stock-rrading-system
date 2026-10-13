from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class BacktestIn(BaseModel):
    strategy: str
    market: str = "US"
    symbols: list[str] = Field(min_length=1)
    start: datetime
    end: datetime
    provider: str = "yfinance"
    initial_cash: dict[str, str] | None = None
    parameters: dict | None = None


class BacktestOut(BaseModel):
    id: int
    status: str
    strategy: str
    market: str
    symbols: list[str]
    metrics: dict | None
    error: str | None
