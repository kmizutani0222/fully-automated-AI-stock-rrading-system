from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = PROJECT_ROOT / "config" / "trading.yaml"


def _decimal(value: object, default: str | None = None) -> Decimal | None:
    if value is None:
        return None if default is None else Decimal(default)
    return Decimal(str(value))


@dataclass(frozen=True)
class TradingConfig:
    ema_fast: int = 9
    ema_slow: int = 21
    rsi_period: int = 14
    rsi_entry: Decimal = Decimal("30")
    rsi_exit: Decimal = Decimal("70")
    stop_loss_pct: Decimal = Decimal("0.0075")
    take_profit_pct: Decimal | None = None
    spread_pct: Decimal = Decimal("0.0002")
    slippage_pct: Decimal = Decimal("0.0005")
    commission_pct: Decimal = Decimal("0")
    risk_per_trade: Decimal = Decimal("0.005")
    daily_loss_pct: Decimal = Decimal("0.02")
    max_drawdown_pct: Decimal = Decimal("0.10")
    max_position_pct: Decimal = Decimal("0.20")
    max_positions: int = 5
    participation_rate: Decimal = Decimal("1")
    us_lot: Decimal = Decimal("0.0001")
    jp_lot: Decimal = Decimal("1")
    strategy_version: str = "v1"
    initial_cash: dict[str, Decimal] | None = None

    def lot_for(self, currency: str) -> Decimal:
        return self.jp_lot if currency == "JPY" else self.us_lot

    def cash(self) -> dict[str, Decimal]:
        raw = self.initial_cash or {"JPY": Decimal("1000000"), "USD": Decimal("0")}
        return {key: Decimal(value) for key, value in raw.items()}


def load_trading_config(path: Path | None = None) -> TradingConfig:
    raw = yaml.safe_load((path or DEFAULT_PATH).read_text(encoding="utf-8")) or {}
    initial = raw.get("initial_cash") or {"JPY": "1000000", "USD": "0"}
    return TradingConfig(
        ema_fast=int(raw.get("ema_fast", 9)),
        ema_slow=int(raw.get("ema_slow", 21)),
        rsi_period=int(raw.get("rsi_period", 14)),
        rsi_entry=_decimal(raw.get("rsi_entry"), "30"),
        rsi_exit=_decimal(raw.get("rsi_exit"), "70"),
        stop_loss_pct=_decimal(raw.get("stop_loss_pct"), "0.0075"),
        take_profit_pct=_decimal(raw.get("take_profit_pct")),
        spread_pct=_decimal(raw.get("spread_pct"), "0.0002"),
        slippage_pct=_decimal(raw.get("slippage_pct"), "0.0005"),
        commission_pct=_decimal(raw.get("commission_pct"), "0"),
        risk_per_trade=_decimal(raw.get("risk_per_trade"), "0.005"),
        daily_loss_pct=_decimal(raw.get("daily_loss_pct"), "0.02"),
        max_drawdown_pct=_decimal(raw.get("max_drawdown_pct"), "0.10"),
        max_position_pct=_decimal(raw.get("max_position_pct"), "0.20"),
        max_positions=int(raw.get("max_positions", 5)),
        participation_rate=_decimal(raw.get("participation_rate"), "1"),
        us_lot=_decimal(raw.get("us_lot"), "0.0001"),
        jp_lot=_decimal(raw.get("jp_lot"), "1"),
        strategy_version=str(raw.get("strategy_version", "v1")),
        initial_cash={key: Decimal(str(value)) for key, value in initial.items()},
    )


def with_overrides(config: TradingConfig, overrides: dict | None) -> TradingConfig:
    if not overrides:
        return config
    allowed = {field for field in config.__dataclass_fields__}
    values = {}
    for key, value in overrides.items():
        if key not in allowed or key == "initial_cash":
            continue
        current = getattr(config, key)
        if isinstance(current, Decimal) or current is None and key.endswith("_pct"):
            values[key] = None if value is None else Decimal(str(value))
        elif isinstance(current, int) and not isinstance(current, bool):
            values[key] = int(value)
        else:
            values[key] = value
    return replace(config, **values)
