from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.trading.config import TradingConfig


@dataclass
class RiskDecision:
    allowed: bool
    quantity: Decimal | None = None
    reason: str = ""


@dataclass
class RiskManager:
    config: TradingConfig
    market: str
    halted: bool = False
    halt_reason: str | None = None
    daily_blocked: bool = False
    peak_equity: dict[str, Decimal] = field(default_factory=dict)
    day_start_equity: dict[str, Decimal] = field(default_factory=dict)
    current_day: date | None = None
    events: list[dict] = field(default_factory=list)

    def _zone(self) -> ZoneInfo:
        return ZoneInfo("Asia/Tokyo" if self.market == "JP" else "America/New_York")

    def record(self, code: str, message: str, timestamp: datetime, details: dict | None = None) -> None:
        self.events.append({"code": code, "message": message, "timestamp": timestamp.isoformat(), "details": details or {}})

    def update_limits(self, currency: str, equity: Decimal, timestamp: datetime) -> None:
        peak = self.peak_equity.get(currency)
        if peak is None or equity > peak:
            self.peak_equity[currency] = equity
            peak = equity
        if peak > 0:
            drawdown = (peak - equity) / peak
            if drawdown >= self.config.max_drawdown_pct and not self.halted:
                self.halted = True
                self.halt_reason = "MAX_DRAWDOWN"
                self.record("MAX_DRAWDOWN", "new entries halted after the drawdown limit", timestamp, {"drawdown": str(drawdown)})
        local_day = timestamp.astimezone(self._zone()).date()
        if self.current_day != local_day:
            self.current_day = local_day
            self.day_start_equity[currency] = equity
            self.daily_blocked = False
        start = self.day_start_equity.get(currency)
        if start and start > 0:
            day_return = (equity - start) / start
            if day_return <= -self.config.daily_loss_pct and not self.daily_blocked:
                self.daily_blocked = True
                self.record("DAILY_LOSS", "new entries halted for the session", timestamp, {"day_return": str(day_return)})

    def size_buy(
        self,
        *,
        equity: Decimal,
        cash: Decimal,
        price: Decimal,
        open_count: int,
        currency: str,
        timestamp: datetime,
    ) -> RiskDecision:
        if self.halted:
            self.record(self.halt_reason or "HALTED", "entry rejected by the circuit breaker", timestamp)
            return RiskDecision(False, reason=self.halt_reason or "HALTED")
        if self.daily_blocked:
            self.record("DAILY_LOSS", "entry rejected by the daily loss limit", timestamp)
            return RiskDecision(False, reason="DAILY_LOSS")
        if open_count >= self.config.max_positions:
            self.record("MAX_POSITIONS", "entry rejected because the position cap is full", timestamp)
            return RiskDecision(False, reason="MAX_POSITIONS")
        if price <= 0 or equity <= 0:
            return RiskDecision(False, reason="INVALID_PRICE")
        risk_notional = equity * self.config.risk_per_trade / self.config.stop_loss_pct
        cap_notional = equity * self.config.max_position_pct
        notional = min(risk_notional, cap_notional)
        buffered = price * (Decimal(1) + self.config.slippage_pct + self.config.spread_pct / Decimal(2))
        if notional > cash:
            self.record("INSUFFICIENT_CASH", "entry rejected because cash cannot fund the sized order", timestamp)
            return RiskDecision(False, reason="INSUFFICIENT_CASH")
        quantity = self._round_lot(notional / buffered, self.config.lot_for(currency))
        if quantity <= 0 or quantity * buffered > cash:
            self.record("INSUFFICIENT_CASH", "entry rejected because the lot size exceeds available cash", timestamp)
            return RiskDecision(False, reason="INSUFFICIENT_CASH")
        return RiskDecision(True, quantity=quantity, reason="ALLOW")

    @staticmethod
    def _round_lot(quantity: Decimal, lot: Decimal) -> Decimal:
        if lot <= 0:
            return quantity
        return (quantity // lot) * lot
