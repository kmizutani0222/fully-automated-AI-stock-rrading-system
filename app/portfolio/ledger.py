from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass
class Position:
    symbol: str
    currency: str
    quantity: Decimal = Decimal(0)
    average_price: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)
    stop_price: Decimal | None = None
    take_profit: Decimal | None = None
    opened_at: datetime | None = None
    closed_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        return self.quantity > 0


@dataclass
class LedgerEntry:
    currency: str
    amount: Decimal
    balance_after: Decimal
    reason: str
    created_at: datetime


@dataclass
class Portfolio:
    """Cash stays in its own currency. A missing FX rate never creates a conversion."""

    cash: dict[str, Decimal]
    positions: dict[str, Position] = field(default_factory=dict)
    reserved: dict[str, Decimal] = field(default_factory=dict)
    ledger: list[LedgerEntry] = field(default_factory=list)
    last_price: dict[str, Decimal] = field(default_factory=dict)

    def available(self, currency: str) -> Decimal:
        return self.cash.get(currency, Decimal(0)) - self.reserved.get(currency, Decimal(0))

    def reserve(self, currency: str, amount: Decimal) -> None:
        self.reserved[currency] = self.reserved.get(currency, Decimal(0)) + amount

    def release(self, currency: str, amount: Decimal) -> None:
        self.reserved[currency] = max(Decimal(0), self.reserved.get(currency, Decimal(0)) - amount)

    def mark(self, symbol: str, price: Decimal) -> None:
        self.last_price[symbol] = price

    def market_value(self, currency: str) -> Decimal:
        total = Decimal(0)
        for position in self.positions.values():
            if position.currency != currency or not position.is_open:
                continue
            price = self.last_price.get(position.symbol)
            if price is None:
                continue
            total += position.quantity * price
        return total

    def equity(self, currency: str) -> Decimal | None:
        if any(position.is_open and position.currency == currency and position.symbol not in self.last_price for position in self.positions.values()):
            return None
        return self.cash.get(currency, Decimal(0)) + self.market_value(currency)

    def reporting_equity(self, quote: str, rates: dict[tuple[str, str], Decimal]) -> Decimal | None:
        total = Decimal(0)
        currencies = set(self.cash) | {position.currency for position in self.positions.values() if position.is_open}
        for currency in currencies:
            equity = self.equity(currency)
            if equity is None:
                return None
            if equity == 0:
                continue
            if currency == quote:
                total += equity
                continue
            rate = rates.get((currency, quote))
            if rate is None:
                return None
            total += equity * rate
        return total

    def apply_buy(
        self,
        symbol: str,
        currency: str,
        quantity: Decimal,
        price: Decimal,
        commission: Decimal,
        filled_at: datetime,
        stop_price: Decimal | None,
        take_profit: Decimal | None,
    ) -> None:
        cost = price * quantity + commission
        if self.cash.get(currency, Decimal(0)) < cost:
            raise ValueError("insufficient cash")
        self.cash[currency] = self.cash.get(currency, Decimal(0)) - cost
        self.ledger.append(LedgerEntry(currency, -cost, self.cash[currency], "BUY", filled_at))
        average_fill = (price * quantity + commission) / quantity
        position = self.positions.get(symbol)
        if position is None or not position.is_open:
            self.positions[symbol] = Position(
                symbol=symbol,
                currency=currency,
                quantity=quantity,
                average_price=average_fill,
                stop_price=stop_price,
                take_profit=take_profit,
                opened_at=filled_at,
            )
            return
        new_quantity = position.quantity + quantity
        position.average_price = (position.average_price * position.quantity + average_fill * quantity) / new_quantity
        position.quantity = new_quantity
        position.stop_price = stop_price
        position.take_profit = take_profit

    def apply_sell(
        self,
        symbol: str,
        quantity: Decimal,
        price: Decimal,
        commission: Decimal,
        filled_at: datetime,
    ) -> Decimal:
        position = self.positions.get(symbol)
        if position is None or position.quantity < quantity:
            raise ValueError("short selling is not enabled")
        proceeds = price * quantity - commission
        realized = (price - position.average_price) * quantity - commission
        self.cash[position.currency] = self.cash.get(position.currency, Decimal(0)) + proceeds
        self.ledger.append(LedgerEntry(position.currency, proceeds, self.cash[position.currency], "SELL", filled_at))
        position.realized_pnl += realized
        position.quantity -= quantity
        if position.quantity == 0:
            position.closed_at = filled_at
            position.stop_price = None
            position.take_profit = None
        return realized
