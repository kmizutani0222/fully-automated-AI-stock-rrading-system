from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from app.backtest.metrics import compute_metrics
from app.broker.simulated import SimulatedBroker
from app.portfolio.ledger import Portfolio
from app.risk.manager import RiskManager
from app.trading.config import TradingConfig
from app.trading.strategies import PriceBar, Signal, Strategy

BAR_INTERVAL = timedelta(minutes=5)


class InsufficientDataError(RuntimeError):
    pass


@dataclass
class EquityPoint:
    timestamp: object
    currency: str
    cash: Decimal
    market_value: Decimal
    equity: Decimal
    drawdown: Decimal | None


@dataclass
class BacktestOutput:
    fills: list
    orders: list
    signals: list[Signal]
    risk_events: list[dict]
    equity: list[EquityPoint]
    metrics: dict
    final_positions: list[dict] = field(default_factory=list)
    ledger: list = field(default_factory=list)


def run_backtest(
    bars_by_symbol: dict[str, list[PriceBar]],
    currencies: dict[str, str],
    strategy: Strategy,
    config: TradingConfig,
    market: str,
    initial_cash: dict[str, Decimal],
) -> BacktestOutput:
    if not bars_by_symbol or any(not rows for rows in bars_by_symbol.values()):
        raise InsufficientDataError("no closed bars for the requested symbols")
    if max(len(rows) for rows in bars_by_symbol.values()) < strategy.minimum_bars:
        raise InsufficientDataError("not enough closed bars for this strategy")
    if not any(_has_next_bar(rows) for rows in bars_by_symbol.values()):
        raise InsufficientDataError("no following bar is available to execute a signal")

    portfolio = Portfolio({key: Decimal(value) for key, value in initial_cash.items()})
    broker = SimulatedBroker(portfolio, config)
    risk = RiskManager(config, market)
    history: dict[str, list[PriceBar]] = {symbol: [] for symbol in bars_by_symbol}
    schedule = {symbol: {bar.timestamp: bar for bar in rows} for symbol, rows in bars_by_symbol.items()}
    timestamps = sorted({bar.timestamp for rows in bars_by_symbol.values() for bar in rows})
    peaks: dict[str, Decimal] = {}
    equity_curve: list[EquityPoint] = []
    signals: list[Signal] = []

    for currency, amount in portfolio.cash.items():
        if amount > 0 and timestamps:
            risk.update_limits(currency, amount, timestamps[0])

    for timestamp in timestamps:
        current = {symbol: schedule[symbol][timestamp] for symbol in schedule if timestamp in schedule[symbol]}
        _execute_due(broker, risk, current, timestamp)
        for symbol, bar in sorted(current.items()):
            broker.fill_limits(bar)
            broker.protective_exit(symbol, bar, timestamp)
            portfolio.mark(symbol, bar.close)
            history[symbol].append(bar)

        _snapshot(portfolio, timestamps, timestamp, peaks, equity_curve, risk)
        planned = _signals(strategy, history, current)
        signals.extend(planned)
        _schedule(planned, broker, risk, portfolio, config, currencies, schedule, timestamp)

    _expire_resting(broker)
    currency = _result_currency(currencies, initial_cash)
    curve = [point.equity for point in equity_curve if point.currency == currency]
    initial = Decimal(initial_cash.get(currency, 0))
    metrics = compute_metrics(broker.executions, curve, initial)
    return BacktestOutput(
        fills=broker.executions,
        orders=list(broker.orders.values()),
        signals=signals,
        risk_events=risk.events,
        equity=equity_curve,
        metrics=metrics,
        final_positions=[
            {
                "symbol": position.symbol,
                "quantity": format(position.quantity, "f"),
                "average_price": format(position.average_price, "f"),
                "realized_pnl": format(position.realized_pnl, "f"),
                "status": "OPEN" if position.is_open else "CLOSED",
            }
            for position in portfolio.positions.values()
        ],
        ledger=list(portfolio.ledger),
    )


def _has_next_bar(rows: list[PriceBar]) -> bool:
    stamps = {bar.timestamp for bar in rows}
    return any(bar.timestamp + BAR_INTERVAL in stamps for bar in rows)


def _execute_due(broker: SimulatedBroker, risk: RiskManager, current: dict[str, PriceBar], timestamp) -> None:
    due = [
        order
        for order in broker.orders.values()
        if order.order_type == "MARKET" and order.execute_at == timestamp and order.status in {"SUBMITTED", "PARTIALLY_FILLED"}
    ]
    for order in sorted(due, key=lambda item: (item.side != "SELL", item.symbol, item.id)):
        bar = current.get(order.symbol)
        if bar is None:
            broker.reject(order, "DATA_GAP")
            risk.record("DATA_GAP", "execution bar is missing", timestamp, {"symbol": order.symbol})
            continue
        broker.fill_market(order, bar)


def _snapshot(portfolio, timestamps, timestamp, peaks, equity_curve, risk) -> None:
    currencies = set(portfolio.cash) | {position.currency for position in portfolio.positions.values()}
    for currency in sorted(currencies):
        equity = portfolio.equity(currency)
        if equity is None:
            continue
        peak = peaks.get(currency, equity)
        if equity > peak:
            peak = equity
        peaks[currency] = peak
        drawdown = (peak - equity) / peak if peak > 0 else Decimal(0)
        equity_curve.append(
            EquityPoint(
                timestamp=timestamp,
                currency=currency,
                cash=portfolio.cash.get(currency, Decimal(0)),
                market_value=portfolio.market_value(currency),
                equity=equity,
                drawdown=drawdown,
            )
        )
        risk.update_limits(currency, equity, timestamp)


def _signals(strategy: Strategy, history: dict[str, list[PriceBar]], current: dict[str, PriceBar]) -> list[Signal]:
    planned: list[Signal] = []
    for symbol in sorted(current):
        signal = strategy.evaluate(history[symbol])
        if signal is not None:
            planned.append(signal)
    return planned


def _schedule(planned, broker, risk, portfolio, config, currencies, schedule, timestamp) -> None:
    sells = [signal for signal in planned if signal.side == "SELL"]
    buys = [signal for signal in planned if signal.side == "BUY"]
    for signal in sells:
        position = portfolio.positions.get(signal.symbol)
        if position is None or not position.is_open:
            continue
        _place(signal, position.quantity, broker, risk, currencies, schedule, timestamp, portfolio)
    candidates = []
    open_names = {item.symbol for item in portfolio.positions.values() if item.is_open}
    pending_names = {
        order.symbol
        for order in broker.orders.values()
        if order.side == "BUY" and order.status in {"SUBMITTED", "PARTIALLY_FILLED"}
    }
    for signal in buys:
        position = portfolio.positions.get(signal.symbol)
        if position is not None and position.is_open:
            continue
        if signal.symbol in pending_names:
            continue
        currency = currencies[signal.symbol]
        price = schedule[signal.symbol][timestamp].close
        equity = portfolio.equity(currency)
        if equity is None:
            continue
        decision = risk.size_buy(
            equity=equity,
            cash=portfolio.available(currency),
            price=price,
            open_count=len(open_names | pending_names),
            currency=currency,
            timestamp=timestamp,
        )
        if not decision.allowed or decision.quantity is None:
            continue
        if len(open_names | pending_names) >= config.max_positions:
            continue
        open_names.add(signal.symbol)
        candidates.append((signal, decision.quantity, currency, price))
    _allocate(candidates, broker, risk, portfolio, config, schedule, timestamp)


def _allocate(candidates, broker, risk, portfolio, config, schedule, timestamp) -> None:
    if not candidates:
        return
    currency = candidates[0][2]
    costs = [(signal, quantity, currency, price, broker._buy_cost(price, quantity)) for signal, quantity, currency, price in candidates]
    affordable = [item for item in costs if item[4] <= portfolio.available(currency)]
    for signal, _quantity, _currency, _price, _cost in costs:
        if _cost > portfolio.available(currency):
            risk.record("INSUFFICIENT_CASH", "entry skipped because it does not fit available cash", timestamp, {"symbol": signal.symbol})
    if len(affordable) > 1:
        total = sum((item[4] for item in affordable), Decimal(0))
        cash = portfolio.available(currency)
        if total > cash and total > 0:
            scale = cash / total
            scaled = []
            for signal, quantity, item_currency, price, _cost in affordable:
                resized = RiskManager._round_lot(quantity * scale, config.lot_for(item_currency))
                if resized > 0:
                    scaled.append((signal, resized, item_currency, price, broker._buy_cost(price, resized)))
            affordable = scaled
    for signal, quantity, item_currency, price, _cost in affordable:
        if broker._buy_cost(price, quantity) > portfolio.available(item_currency):
            risk.record("INSUFFICIENT_CASH", "scaled entry no longer fits available cash", timestamp, {"symbol": signal.symbol})
            continue
        _place(signal, quantity, broker, risk, {signal.symbol: item_currency}, schedule, timestamp, portfolio)


def _place(signal: Signal, quantity: Decimal, broker, risk, currencies, schedule, timestamp, portfolio) -> None:
    execute_at = timestamp + BAR_INTERVAL
    if execute_at not in schedule[signal.symbol]:
        risk.record("DATA_GAP", "next 5-minute bar is missing, so the signal was not filled", timestamp, {"symbol": signal.symbol})
        return
    reference = schedule[signal.symbol][timestamp].close
    broker.submit_market(
        symbol=signal.symbol,
        side=signal.side,
        quantity=quantity,
        currency=currencies[signal.symbol],
        execute_at=execute_at,
        idempotency_key=f"{signal.symbol}:{timestamp.isoformat()}:{signal.side}",
        reserve_price=reference,
        created_at=timestamp,
    )


def _expire_resting(broker: SimulatedBroker) -> None:
    for order in list(broker.orders.values()):
        if order.status in {"SUBMITTED", "PARTIALLY_FILLED"}:
            broker.expire(order)


def _result_currency(currencies: dict[str, str], initial_cash: dict[str, Decimal]) -> str:
    if currencies:
        return currencies[sorted(currencies)[0]]
    return next(iter(initial_cash))
