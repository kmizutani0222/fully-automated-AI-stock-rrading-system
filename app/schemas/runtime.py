from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class RuntimeCommand(BaseModel):
    mode: str | None = None
    account_id: int | None = None


class AccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    base_currency: str = "JPY"
    jpy_cash: Decimal = Decimal("0")
    usd_cash: Decimal = Decimal("0")
    markets: list[str] = Field(default_factory=lambda: ["US"])
    strategies: list[str] = Field(default_factory=lambda: ["ema_cross"])
    symbols: list[str] = Field(default_factory=lambda: ["AAPL"])
    mode: str = "PAUSED"
    fallback_to_rules: bool = False
    risk_settings: dict = Field(default_factory=dict)


class AccountSettingsIn(BaseModel):
    markets: list[str] | None = None
    strategies: list[str] | None = None
    symbols: list[str] | None = None
    mode: str | None = None
    fallback_to_rules: bool | None = None
    risk_settings: dict | None = None


class CashIn(BaseModel):
    currency: str
    amount: Decimal
    reason: str


class AccountOut(BaseModel):
    id: int
    name: str
    base_currency: str
    markets: list
    mode: str
    strategies: list
    symbols: list
    fallback_to_rules: bool
    cash: dict[str, str]
    created_at: datetime
