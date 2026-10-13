from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class TrainIn(BaseModel):
    task: str
    market: str = "US"
    symbols: list[str] = Field(min_length=1)
    start: datetime
    end: datetime
    provider: str = "yfinance"
    index_symbol: str | None = None
    parameters: dict | None = None


class AiBacktestIn(BaseModel):
    market: str = "US"
    symbols: list[str] = Field(min_length=1)
    start: datetime
    end: datetime
    provider: str = "yfinance"
    initial_cash: dict[str, str] | None = None
    parameters: dict | None = None
