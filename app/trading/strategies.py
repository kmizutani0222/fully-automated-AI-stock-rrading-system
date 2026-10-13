from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.trading.config import TradingConfig
from app.trading.indicators import ema, rsi


@dataclass(frozen=True)
class PriceBar:
    symbol: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None = None


@dataclass(frozen=True)
class Signal:
    symbol: str
    timestamp: datetime
    side: str
    reason: str


class Strategy(ABC):
    name: str
    version: str

    @property
    @abstractmethod
    def minimum_bars(self) -> int:
        pass

    @abstractmethod
    def evaluate(self, bars: list[PriceBar]) -> Signal | None:
        """Signal from bars that are already closed. The last bar is the decision bar."""


class EmaCrossStrategy(Strategy):
    name = "ema_cross"

    def __init__(self, config: TradingConfig) -> None:
        self.version = config.strategy_version
        self.fast = config.ema_fast
        self.slow = config.ema_slow

    @property
    def minimum_bars(self) -> int:
        return self.slow + 1

    def evaluate(self, bars: list[PriceBar]) -> Signal | None:
        if len(bars) < self.minimum_bars:
            return None
        closes = [bar.close for bar in bars]
        fast = ema(closes, self.fast)
        slow = ema(closes, self.slow)
        index = len(bars) - 1
        previous = index - 1
        if None in {fast[index], slow[index], fast[previous], slow[previous]}:
            return None
        crossed_up = fast[previous] <= slow[previous] and fast[index] > slow[index]
        crossed_down = fast[previous] >= slow[previous] and fast[index] < slow[index]
        if crossed_up:
            return Signal(bars[-1].symbol, bars[-1].timestamp, "BUY", "ema_9_crossed_above_ema_21")
        if crossed_down:
            return Signal(bars[-1].symbol, bars[-1].timestamp, "SELL", "ema_9_crossed_below_ema_21")
        return None


class RsiReversionStrategy(Strategy):
    name = "rsi_reversion"

    def __init__(self, config: TradingConfig) -> None:
        self.version = config.strategy_version
        self.period = config.rsi_period
        self.entry = config.rsi_entry
        self.exit = config.rsi_exit

    @property
    def minimum_bars(self) -> int:
        return self.period + 2

    def evaluate(self, bars: list[PriceBar]) -> Signal | None:
        if len(bars) < self.minimum_bars:
            return None
        values = rsi([bar.close for bar in bars], self.period)
        current = values[-1]
        previous = values[-2]
        if current is None or previous is None:
            return None
        if previous < self.entry and current >= self.entry:
            return Signal(bars[-1].symbol, bars[-1].timestamp, "BUY", "rsi_recovered_above_30")
        if current >= self.exit:
            return Signal(bars[-1].symbol, bars[-1].timestamp, "SELL", "rsi_at_or_above_70")
        return None


class BuyHoldStrategy(Strategy):
    name = "buy_hold"

    def __init__(self, config: TradingConfig) -> None:
        self.version = config.strategy_version
        self._entered: set[str] = set()

    @property
    def minimum_bars(self) -> int:
        return 1

    def evaluate(self, bars: list[PriceBar]) -> Signal | None:
        if not bars:
            return None
        symbol = bars[-1].symbol
        if symbol in self._entered:
            return None
        self._entered.add(symbol)
        return Signal(symbol, bars[-1].timestamp, "BUY", "buy_and_hold_entry")


def build_strategy(name: str, config: TradingConfig) -> Strategy:
    factories = {
        EmaCrossStrategy.name: EmaCrossStrategy,
        RsiReversionStrategy.name: RsiReversionStrategy,
        BuyHoldStrategy.name: BuyHoldStrategy,
    }
    if name not in factories:
        raise KeyError(name)
    return factories[name](config)


def strategy_names() -> list[str]:
    return ["ema_cross", "rsi_reversion", "buy_hold"]
