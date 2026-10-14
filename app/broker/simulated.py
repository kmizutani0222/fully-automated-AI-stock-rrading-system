from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.portfolio.ledger import Portfolio
from app.trading.config import TradingConfig
from app.trading.strategies import PriceBar


@dataclass
class Execution:
    order_id: int
    sequence: int
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    reference_price: Decimal
    commission: Decimal
    slippage_cost: Decimal
    spread_cost: Decimal
    realized_pnl: Decimal | None
    executed_at: datetime
    signal_time: datetime
    fill_observed_at: datetime | None = None
    filled_at: datetime | None = None


@dataclass
class BrokerOrder:
    id: int
    symbol: str
    side: str
    order_type: str
    quantity: Decimal
    filled_quantity: Decimal
    status: str
    idempotency_key: str
    currency: str
    created_at: datetime
    limit_price: Decimal | None = None
    execute_at: datetime | None = None
    reserved: Decimal = Decimal(0)
    events: list[str] = field(default_factory=list)
    submitted_at: datetime | None = None
    signal_generated_at: datetime | None = None
    bar_close_time: datetime | None = None
    data_received_at: datetime | None = None
    strategy_name: str | None = None
    decision_reason: str | None = None

    @property
    def remaining(self) -> Decimal:
        return self.quantity - self.filled_quantity


class SimulatedBroker:
    """5-minute bars have no queue. Fills are pessimistic, and a filled order cannot fill again."""

    def __init__(self, portfolio: Portfolio, config: TradingConfig) -> None:
        self.portfolio = portfolio
        self.config = config
        self.orders: dict[int, BrokerOrder] = {}
        self.executions: list[Execution] = []
        self._by_key: dict[str, BrokerOrder] = {}
        self._next_id = 1
        self._sequences: dict[int, int] = {}

    def submit_market(
        self,
        *,
        symbol: str,
        side: str,
        quantity: Decimal,
        currency: str,
        execute_at: datetime,
        idempotency_key: str,
        reserve_price: Decimal,
        created_at: datetime,
    ) -> BrokerOrder:
        existing = self._by_key.get(idempotency_key)
        if existing is not None:
            return existing
        order = self._new(symbol, side, "MARKET", quantity, currency, idempotency_key, created_at)
        order.execute_at = execute_at
        order.status = "SUBMITTED"
        order.events.append("SUBMITTED")
        if side == "BUY":
            order.reserved = self._buy_cost(reserve_price, quantity)
            self.portfolio.reserve(currency, order.reserved)
        return order

    def submit_limit(
        self,
        *,
        symbol: str,
        side: str,
        quantity: Decimal,
        currency: str,
        limit_price: Decimal,
        idempotency_key: str,
        created_at: datetime,
    ) -> BrokerOrder:
        existing = self._by_key.get(idempotency_key)
        if existing is not None:
            return existing
        order = self._new(symbol, side, "LIMIT", quantity, currency, idempotency_key, created_at)
        order.limit_price = limit_price
        order.status = "SUBMITTED"
        order.events.append("SUBMITTED")
        if side == "BUY":
            order.reserved = self._buy_cost(limit_price, quantity)
            self.portfolio.reserve(currency, order.reserved)
        return order

    def cancel(self, order_id: int) -> BrokerOrder:
        order = self.orders[order_id]
        if order.status in {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}:
            return order
        order.status = "CANCELED"
        order.events.append("CANCELED")
        self._release(order)
        return order

    def expire(self, order: BrokerOrder) -> None:
        if order.status in {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}:
            return
        order.status = "EXPIRED"
        order.events.append("EXPIRED")
        self._release(order)

    def reject(self, order: BrokerOrder, reason: str) -> None:
        if order.status in {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}:
            return
        order.status = "REJECTED"
        order.events.append(reason)
        self._release(order)

    def fill_market(self, order: BrokerOrder, bar: PriceBar) -> Execution | None:
        if order.status == "FILLED":
            return None
        if order.status not in {"SUBMITTED", "PARTIALLY_FILLED"}:
            return None
        if order.execute_at != bar.timestamp:
            return None
        return self._fill(order, bar, self._market_price(order.side, bar.open), bar.open)

    def fill_observed(self, order: BrokerOrder, bar: PriceBar, price: Decimal, reference: Decimal, observed_at: datetime) -> Execution | None:
        """Paper fill. The caller must pass a price observed after submission. Backtests keep fill_market."""
        submitted = order.submitted_at or order.created_at
        if observed_at <= submitted:
            return None
        if order.status not in {"SUBMITTED", "PARTIALLY_FILLED"}:
            return None
        execution = self._fill(order, bar, price, reference)
        if execution is not None:
            execution.fill_observed_at = observed_at
            execution.filled_at = observed_at
        return execution

    def fill_limits(self, bar: PriceBar) -> list[Execution]:
        fills: list[Execution] = []
        for order in list(self.orders.values()):
            if order.order_type != "LIMIT" or order.symbol != bar.symbol:
                continue
            if order.status not in {"SUBMITTED", "PARTIALLY_FILLED"} or order.limit_price is None:
                continue
            price = self._limit_price(order, bar)
            if price is None:
                continue
            fill = self._fill(order, bar, price, bar.open)
            if fill is not None:
                fills.append(fill)
        return fills

    def protective_exit(self, symbol: str, bar: PriceBar, created_at: datetime) -> Execution | None:
        position = self.portfolio.positions.get(symbol)
        if position is None or not position.is_open or position.stop_price is None:
            return None
        price, reason = self._protective_price(position.stop_price, position.take_profit, bar)
        if price is None:
            return None
        order = self.submit_market(
            symbol=symbol,
            side="SELL",
            quantity=position.quantity,
            currency=position.currency,
            execute_at=bar.timestamp,
            idempotency_key=f"protective:{symbol}:{bar.timestamp.isoformat()}",
            reserve_price=price,
            created_at=created_at,
        )
        order.events.append(reason)
        return self._fill(order, bar, price, price)

    def _protective_price(self, stop: Decimal, target: Decimal | None, bar: PriceBar) -> tuple[Decimal, str] | tuple[None, str]:
        if bar.open <= stop:
            return self._sell_price(bar.open), "STOP_GAP"
        hit_stop = bar.low <= stop
        hit_target = target is not None and bar.high >= target
        if hit_stop:
            return self._sell_price(stop), "STOP_BEFORE_TARGET" if hit_target else "STOP"
        if hit_target and target is not None:
            return self._sell_price(target), "TAKE_PROFIT"
        return None, ""

    def _fill(self, order: BrokerOrder, bar: PriceBar, price: Decimal, reference: Decimal) -> Execution | None:
        if order.status == "FILLED" or order.remaining <= 0:
            return None
        if any(item.order_id == order.id and item.executed_at == bar.timestamp for item in self.executions):
            return None
        quantity = self._capacity(order.remaining, bar.volume)
        if quantity <= 0:
            return None
        commission = price * quantity * self.config.commission_pct
        position = self.portfolio.positions.get(order.symbol)
        try:
            if order.side == "BUY":
                stop = price * (Decimal(1) - self.config.stop_loss_pct)
                target = None if self.config.take_profit_pct is None else price * (Decimal(1) + self.config.take_profit_pct)
                self.portfolio.apply_buy(order.symbol, order.currency, quantity, price, commission, bar.timestamp, stop, target)
                realized = None
            else:
                if position is None:
                    raise ValueError("short selling is not enabled")
                realized = self.portfolio.apply_sell(order.symbol, quantity, price, commission, bar.timestamp)
        except ValueError:
            self.reject(order, "REJECTED_INSUFFICIENT_CASH")
            return None
        self._release(order)
        order.filled_quantity += quantity
        if order.remaining > 0:
            order.status = "PARTIALLY_FILLED"
            if order.side == "BUY":
                order.reserved = self._buy_cost(price, order.remaining)
                self.portfolio.reserve(order.currency, order.reserved)
        else:
            order.status = "FILLED"
        order.events.append(order.status)
        sequence = self._sequences.get(order.id, 0) + 1
        self._sequences[order.id] = sequence
        slippage = abs(price - reference) * quantity
        spread = reference * self.config.spread_pct / Decimal(2) * quantity
        execution = Execution(
            order_id=order.id,
            sequence=sequence,
            symbol=order.symbol,
            side=order.side,
            quantity=quantity,
            price=price,
            reference_price=reference,
            commission=commission,
            slippage_cost=slippage,
            spread_cost=spread,
            realized_pnl=realized,
            executed_at=bar.timestamp,
            signal_time=order.created_at,
        )
        self.executions.append(execution)
        return execution

    def _capacity(self, remaining: Decimal, volume: Decimal | None) -> Decimal:
        if self.config.participation_rate >= 1 or volume is None:
            return remaining
        tradable = volume * self.config.participation_rate
        return min(remaining, tradable)

    def _market_price(self, side: str, open_price: Decimal) -> Decimal:
        return self._buy_price(open_price) if side == "BUY" else self._sell_price(open_price)

    def _limit_price(self, order: BrokerOrder, bar: PriceBar) -> Decimal | None:
        limit = order.limit_price
        if limit is None:
            return None
        if order.side == "BUY":
            if bar.low > limit:
                return None
            worsened = self._buy_price(bar.open)
            if bar.open <= limit and worsened <= limit:
                return worsened
            return limit
        if bar.high < limit:
            return None
        worsened = self._sell_price(bar.open)
        if bar.open >= limit and worsened >= limit:
            return worsened
        return limit

    def _buy_price(self, reference: Decimal) -> Decimal:
        return reference * (Decimal(1) + self.config.slippage_pct + self.config.spread_pct / Decimal(2))

    def _sell_price(self, reference: Decimal) -> Decimal:
        return reference * (Decimal(1) - self.config.slippage_pct - self.config.spread_pct / Decimal(2))

    def _buy_cost(self, price: Decimal, quantity: Decimal) -> Decimal:
        fill = self._buy_price(price)
        return fill * quantity + fill * quantity * self.config.commission_pct

    def _new(self, symbol: str, side: str, order_type: str, quantity: Decimal, currency: str, key: str, created_at: datetime) -> BrokerOrder:
        order = BrokerOrder(
            id=self._next_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            filled_quantity=Decimal(0),
            status="CREATED",
            idempotency_key=key,
            currency=currency,
            created_at=created_at,
        )
        self._next_id += 1
        order.events.append("CREATED")
        self.orders[order.id] = order
        self._by_key[key] = order
        return order

    def _release(self, order: BrokerOrder) -> None:
        if order.reserved > 0:
            self.portfolio.release(order.currency, order.reserved)
            order.reserved = Decimal(0)
