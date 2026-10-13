from __future__ import annotations

from pydantic import BaseModel, Field


class InstrumentOut(BaseModel):
    market: str
    symbol: str
    name: str | None
    exchange: str | None
    currency: str
    active: bool


class InstrumentIn(BaseModel):
    market: str
    symbol: str = Field(min_length=1, max_length=30)
    name: str | None = None
    exchange: str | None = None
    currency: str | None = None
