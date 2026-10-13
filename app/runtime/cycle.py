from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.config import load_ai_config
from app.ai.features import compute_features
from app.ai.regimes import RuleRegimeClassifier
from app.broker.simulated import BrokerOrder, SimulatedBroker
from app.data.bar import NormalizedBar
from app.data.providers.base import ProviderError
from app.data.providers.yfinance_provider import YFinanceUsProvider
from app.data.quality import drop_open_bars, partition_bars
from app.data.repository import ensure_instrument, get_instrument, load_bars
from app.models import Instrument, MarketBar
from app.models.ai import FeatureSnapshot, MarketRegime, ModelVersion, PredictionRecord, StrategyDecision
from app.models.runtime import JobExecution, NotificationEvent, ProcessingCheckpoint, SchedulerJob, TradingSession
from app.models.trading import Account, CashLedgerEntry, ExecutionRecord, OrderEvent, OrderRecord, PortfolioSnapshot, PositionRecord, RiskEvent
from app.portfolio.ledger import Portfolio, Position
from app.risk.manager import RiskManager
from app.runtime.accounts import cash_balances, latest_session
from app.ai.calendar import zone_for
from app.runtime.schedule import FILL_MODEL, bar_ready, due_bars, market_day
from app.trading.config import load_trading_config, with_overrides
from app.trading.strategies import PriceBar, build_strategy

CYCLE_LOCK = threading.Lock()
JOB_NAME = "paper"
STALE_LOCK = timedelta(seconds=90)
OPEN_ORDER_STATUSES = {"SUBMITTED", "PARTIALLY_FILLED"}


@dataclass
class CycleResult:
    status: str
    reason: str | None = None
    processed: list[str] = field(default_factory=list)
    orders: int = 0


class StaticSource:
    def __init__(self, bars: list[NormalizedBar]) -> None:
        self.bars = bars

    def fetch(self, market: str, symbol: str, start: datetime, end: datetime) -> list[NormalizedBar]:
        return [
            bar
            for bar in self.bars
            if bar.market == market and bar.symbol == symbol and start <= bar.timestamp < end
        ]


class YFinanceSource:
    """US bars from Yahoo. A failed request raises and never invents a price. JP has no online source."""

    def fetch(self, market: str, symbol: str, start: datetime, end: datetime) -> list[NormalizedBar]:
        if market != "US":
            raise ProviderError("Japan equities have no online 5-minute source; automatic trading stays off")
        import asyncio

        provider = YFinanceUsProvider()
        return asyncio.run(provider.get_bars(symbol, "5m", start, end))


def run_cycle(
    session: Session,
    now: datetime,
    *,
    account_name: str = "paper",
    source: object | None = None,
    delay_seconds: int | None = None,
    job_name: str = JOB_NAME,
) -> CycleResult:
    if not CYCLE_LOCK.acquire(blocking=False):
        return CycleResult("SKIPPED", "already_running")
    try:
        return _run(session, now, account_name=account_name, source=source, delay_seconds=delay_seconds, job_name=job_name)
    except Exception as exc:  # noqa: BLE001 - a failed cycle must not place a new order
        session.rollback()
        _record_failure(account_name, str(exc), job_name)
        return CycleResult("FAILED", str(exc))
    finally:
        CYCLE_LOCK.release()


def _run(session, now, *, account_name: str, source: object | None, delay_seconds: int | None, job_name: str) -> CycleResult:
    current = _aware(now)
    delay = delay_seconds if delay_seconds is not None else 30
    account = session.scalar(select(Account).where(Account.name == account_name))
    if account is None:
        return CycleResult("FAILED", "paper account is missing")
    job = _lock_job(session, current, job_name)
    if job is None:
        session.rollback()
        return CycleResult("SKIPPED", "already_running")
    started = current
    try:
        result = _cycle_account(session, account, current, source or YFinanceSource(), delay)
        job.status = "IDLE"
        job.heartbeat_at = datetime.now(timezone.utc)
        job.last_error = None if result.status != "FAILED" else result.reason
        session.add(
            JobExecution(
                job_id=job.id,
                status=result.status,
                reason=result.reason,
                started_at=started,
                finished_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
        return result
    except Exception:
        session.rollback()
        raise


def _cycle_account(session, account: Account, now: datetime, source, delay: int) -> CycleResult:
    _ensure_session(session, account, now)
    if "JP" in {str(market).upper() for market in account.markets}:
        _notify(session, account.id, "WARNING", "jp_auto_disabled", "日本株のオンライン取得が無いため、自動売買は行いません。CSVのバックテストだけが使えます。")
    markets = [str(market).upper() for market in account.markets if str(market).upper() != "JP"]
    if not markets:
        return CycleResult("SKIPPED", "no automatic market is enabled")
    config = with_overrides(load_trading_config(), account.risk_settings or {})
    broker, portfolio = _load_book(session, account, config)
    instruments = _instruments(session, account)
    processed: list[str] = []
    created_orders = 0
    for market in markets:
        due = due_bars(market, now, delay)
        if not due:
            _notify(session, account.id, "INFO", "market_closed", f"{market} は {market_day(market, now).isoformat()} に取引対象の確定足がありません。")
            continue
        for symbol in _symbols_for(account, market, instruments):
            last = _last_checkpoint(session, account.id, market, symbol)
            if last is None:
                today = [bar for bar in due if bar.astimezone(zone_for(market)).date() == market_day(market, now)]
                if not today:
                    _notify(session, account.id, "INFO", "market_closed", f"{market} は {market_day(market, now).isoformat()} に取引対象の確定足がありません。")
                    continue
                _mark_checkpoint(session, account.id, market, symbol, today[-1])
                _notify(session, account.id, "INFO", "checkpoint_initialized", f"{symbol} の処理位置を {today[-1].isoformat()} に合わせました。過去足は売買しません。")
                processed.append(f"{symbol}:anchor")
                continue
            pending = [bar for bar in due if bar > last]
            if not pending:
                continue
            window_start = pending[0]
            window_end = pending[-1] + timedelta(minutes=5)
            try:
                fetched = source.fetch(market, symbol, window_start, window_end)
            except Exception as exc:  # noqa: BLE001
                _notify(session, account.id, "ERROR", "data_fetch_failed", f"{symbol} の価格は取得できませんでした。仮の価格は作りません。{exc}")
                raise ProviderError(str(exc)) from exc
            usable = _accept_bars(session, fetched, now)
            by_time = {bar.timestamp: bar for bar in usable if bar.symbol == symbol and bar.market == market}
            for bar_start in pending:
                if not bar_ready(bar_start, now, delay):
                    break
                bar = by_time.get(bar_start)
                if bar is None:
                    _notify(session, account.id, "ERROR", "data_missing", f"{symbol} の {bar_start.isoformat()} が無いため処理を止めました。")
                    raise ProviderError(f"missing bar {symbol} {bar_start.isoformat()}")
                history = _history(session, instruments[symbol].id, bar_start)
                _features(session, instruments[symbol], history, now)
                new_orders = _trade_bar(session, account, broker, portfolio, config, market, history, bar_start, instruments)
                created_orders += new_orders
                _mark_checkpoint(session, account.id, market, symbol, bar_start)
                _snapshot(session, account.id, portfolio, broker, bar_start)
                processed.append(f"{symbol}:{bar_start.isoformat()}")
    _sync(session, account, broker, instruments, now)
    _reconcile(session, account.id, portfolio)
    if not processed:
        return CycleResult("SKIPPED", "no new closed bar", orders=created_orders)
    return CycleResult("SUCCEEDED", None, processed, created_orders)


def _trade_bar(session, account, broker: SimulatedBroker, portfolio: Portfolio, config, market, history, bar_start, instruments) -> int:
    bar = history[-1]
    for order in list(broker.orders.values()):
        if order.symbol != bar.symbol or order.execute_at != bar.timestamp:
            continue
        if order.execute_at is None or order.execute_at <= order.created_at:
            broker.reject(order, "FILL_BEFORE_SIGNAL")
            continue
        if _execution_exists(session, order.idempotency_key, bar.timestamp):
            continue
        broker.fill_market(order, bar)
    broker.fill_limits(bar)
    broker.protective_exit(bar.symbol, bar, bar.timestamp)
    portfolio.mark(bar.symbol, bar.close)
    engine = _engine(session, account)
    choice = "CASH"
    reason = "no_new_entry"
    if engine is None:
        reason = _idle_reason(account, session)
    else:
        signal = _signal(session, account, engine, history, bar, instruments)
        if signal is None:
            reason = "no_signal"
        elif signal.side == "SELL":
            choice, reason = _submit_exit(session, account, broker, portfolio, signal, bar)
        elif signal.side == "BUY":
            choice, reason = _submit_entry(session, broker, portfolio, config, market, signal, bar, account)
        else:
            reason = "signal_ignored"
    _decision(session, instruments[bar.symbol].id, bar.timestamp, choice, reason, engine)
    return 1 if choice in {"DAY", "RULE"} else 0


def _submit_entry(session, broker, portfolio, config, market, signal, bar, account) -> tuple[str, str]:
    if _pending_buy(broker, signal.symbol) or _open_position(portfolio, signal.symbol):
        return "CASH", "position_or_order_exists"
    key = _order_key(account.id, signal.symbol, signal.timestamp, "BUY")
    if session.scalar(select(OrderRecord.id).where(OrderRecord.idempotency_key == key)) is not None:
        return "CASH", "duplicate_order"
    currency = _currency(session, signal.symbol, market)
    equity = portfolio.equity(currency)
    if equity is None:
        return "CASH", "equity_unknown"
    risk = RiskManager(config, market)
    decision = risk.size_buy(
        equity=equity,
        cash=portfolio.available(currency),
        price=bar.close,
        open_count=sum(1 for position in portfolio.positions.values() if position.is_open),
        currency=currency,
        timestamp=bar.timestamp,
    )
    for event in risk.events:
        session.add(RiskEvent(code=event["code"], message=event["message"], details={"account_id": account.id, **event["details"]}))
    if not decision.allowed or decision.quantity is None:
        return "CASH", decision.reason or "risk_rejected"
    execute_at = bar.timestamp + timedelta(minutes=5)
    if execute_at <= signal.timestamp:
        return "CASH", "fill_not_after_signal"
    broker.submit_market(
        symbol=signal.symbol,
        side="BUY",
        quantity=decision.quantity,
        currency=currency,
        execute_at=execute_at,
        idempotency_key=key,
        reserve_price=bar.close,
        created_at=signal.timestamp,
    )
    return "RULE" if account.mode == "RULE_BASED" else "DAY", signal.reason


def _submit_exit(session, account, broker, portfolio, signal, bar) -> tuple[str, str]:
    position = portfolio.positions.get(signal.symbol)
    if position is None or not position.is_open:
        return "CASH", "no_open_position"
    key = _order_key(account.id, signal.symbol, signal.timestamp, "SELL")
    if broker._by_key.get(key) is not None or session.scalar(select(OrderRecord.id).where(OrderRecord.idempotency_key == key)) is not None:
        return "CASH", "duplicate_order"
    execute_at = bar.timestamp + timedelta(minutes=5)
    broker.submit_market(
        symbol=signal.symbol,
        side="SELL",
        quantity=position.quantity,
        currency=position.currency,
        execute_at=execute_at,
        idempotency_key=key,
        reserve_price=bar.close,
        created_at=signal.timestamp,
    )
    return "CASH", signal.reason


def _signal(session, account, engine, history, bar, instruments):
    if engine == "rules":
        name = (account.strategies or ["ema_cross"])[0]
        strategy = build_strategy(name, with_overrides(load_trading_config(), account.risk_settings or {}))
        return strategy.evaluate(history)
    predictions = session.scalars(
        select(PredictionRecord)
        .where(
            PredictionRecord.instrument_id == instruments[bar.symbol].id,
            PredictionRecord.is_out_of_sample.is_(True),
            PredictionRecord.available_at <= bar.timestamp + timedelta(minutes=5),
            PredictionRecord.expires_at > bar.timestamp,
        )
        .order_by(PredictionRecord.id.desc())
    ).all()
    if not predictions:
        return None
    best = predictions[0]
    if best.expected_return is None or best.expected_return <= 0:
        return None
    from app.trading.strategies import Signal

    return Signal(bar.symbol, bar.timestamp, "BUY", f"ai:{best.horizon}")


def _engine(session, account: Account) -> str | None:
    if account.mode == "PAUSED":
        return None
    active = session.scalar(select(ModelVersion.id).where(ModelVersion.status == "ACTIVE").limit(1))
    if account.mode == "RULE_BASED":
        return "rules"
    if account.mode == "AI":
        return "ai" if active is not None else None
    if account.mode == "HYBRID":
        if active is not None:
            return "ai"
        return "rules" if account.fallback_to_rules else None
    return None


def _idle_reason(account, session) -> str:
    if account.mode == "PAUSED":
        return "paused"
    if account.mode == "AI":
        return "model_unavailable"
    if account.mode == "HYBRID" and not account.fallback_to_rules:
        return "fallback_not_allowed"
    return "idle"


def _decision(session, instrument_id, timestamp, choice, reason, engine) -> None:
    if account_decision_exists(session, instrument_id, timestamp):
        return
    session.add(
        StrategyDecision(
            instrument_id=instrument_id,
            timestamp=timestamp,
            choice=choice,
            reason=reason,
            details={"engine": engine},
        )
    )


def account_decision_exists(session, instrument_id, timestamp) -> bool:
    return (
        session.scalar(
            select(StrategyDecision.id).where(StrategyDecision.instrument_id == instrument_id, StrategyDecision.timestamp == timestamp)
        )
        is not None
    )


def _load_book(session, account, config) -> tuple[SimulatedBroker, Portfolio]:
    balances = cash_balances(session, account.id)
    balances.setdefault("JPY", Decimal(0))
    balances.setdefault("USD", Decimal(0))
    portfolio = Portfolio({key: Decimal(value) for key, value in balances.items()})
    positions = session.scalars(
        select(PositionRecord).where(
            PositionRecord.account_id == account.id,
            PositionRecord.backtest_run_id.is_(None),
            PositionRecord.status == "OPEN",
        )
    ).all()
    for row in positions:
        instrument = session.get(Instrument, row.instrument_id)
        if instrument is None:
            continue
        portfolio.positions[instrument.symbol] = Position(
            symbol=instrument.symbol,
            currency=instrument.currency,
            quantity=row.quantity,
            average_price=row.average_price,
            realized_pnl=row.realized_pnl,
            opened_at=row.opened_at,
        )
    broker = SimulatedBroker(portfolio, config)
    orders = session.scalars(
        select(OrderRecord).where(OrderRecord.account_id == account.id, OrderRecord.status.in_(OPEN_ORDER_STATUSES))
    ).all()
    max_id = 0
    for row in orders:
        instrument = session.get(Instrument, row.instrument_id)
        if instrument is None or row.execute_at is None:
            continue
        order = BrokerOrder(
            id=row.id,
            symbol=instrument.symbol,
            side=row.side,
            order_type=row.order_type,
            quantity=row.quantity,
            filled_quantity=row.filled_quantity,
            status=row.status,
            idempotency_key=row.idempotency_key,
            currency=instrument.currency,
            created_at=row.created_at,
            limit_price=row.limit_price,
            execute_at=row.execute_at,
            reserved=row.reserved_cash or Decimal(0),
        )
        broker.orders[order.id] = order
        broker._by_key[order.idempotency_key] = order
        max_id = max(max_id, row.id)
        if order.side == "BUY" and order.reserved > 0:
            portfolio.reserve(order.currency, order.reserved)
    broker._next_id = max_id + 1
    return broker, portfolio


def _sync(session, account, broker: SimulatedBroker, instruments, now: datetime) -> None:
    id_map: dict[int, int] = {}
    for order in broker.orders.values():
        row = session.scalar(select(OrderRecord).where(OrderRecord.idempotency_key == order.idempotency_key))
        if row is None:
            row = OrderRecord(
                account_id=account.id,
                backtest_run_id=None,
                instrument_id=instruments[order.symbol].id,
                side=order.side,
                order_type=order.order_type,
                quantity=order.quantity,
                filled_quantity=order.filled_quantity,
                limit_price=order.limit_price,
                status=order.status,
                idempotency_key=order.idempotency_key,
                execute_at=order.execute_at,
                reserved_cash=order.reserved,
                created_at=order.created_at,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            for event in order.events:
                session.add(OrderEvent(order_id=row.id, status=order.status, message=event))
        else:
            row.filled_quantity = order.filled_quantity
            row.status = order.status
            row.reserved_cash = order.reserved
            row.execute_at = order.execute_at
            row.updated_at = now
        id_map[order.id] = row.id
    for execution in broker.executions:
        order_id = id_map[execution.order_id]
        exists = session.scalar(
            select(ExecutionRecord.id).where(ExecutionRecord.order_id == order_id, ExecutionRecord.sequence == execution.sequence)
        )
        if exists is not None:
            continue
        session.add(
            ExecutionRecord(
                order_id=order_id,
                sequence=execution.sequence,
                quantity=execution.quantity,
                price=execution.price,
                commission=execution.commission,
                slippage_cost=execution.slippage_cost,
                spread_cost=execution.spread_cost,
                executed_at=execution.executed_at,
            )
        )
    for entry in broker.portfolio.ledger:
        session.add(
            CashLedgerEntry(
                account_id=account.id,
                currency=entry.currency,
                amount=entry.amount,
                balance_after=entry.balance_after,
                reason=entry.reason,
                created_at=entry.created_at,
            )
        )
    for symbol, position in broker.portfolio.positions.items():
        instrument = instruments.get(symbol)
        if instrument is None:
            continue
        row = session.scalar(
            select(PositionRecord).where(
                PositionRecord.account_id == account.id,
                PositionRecord.instrument_id == instrument.id,
                PositionRecord.backtest_run_id.is_(None),
                PositionRecord.status == "OPEN",
            )
        )
        if position.is_open:
            if row is None:
                session.add(
                    PositionRecord(
                        account_id=account.id,
                        instrument_id=instrument.id,
                        quantity=position.quantity,
                        average_price=position.average_price,
                        realized_pnl=position.realized_pnl,
                        status="OPEN",
                        opened_at=position.opened_at,
                    )
                )
            else:
                row.quantity = position.quantity
                row.average_price = position.average_price
                row.realized_pnl = position.realized_pnl
        elif row is not None:
            row.quantity = Decimal(0)
            row.status = "CLOSED"
            row.closed_at = position.closed_at
            row.realized_pnl = position.realized_pnl


def _reconcile(session, account_id: int, portfolio: Portfolio) -> None:
    session.flush()
    balances = cash_balances(session, account_id)
    for currency, amount in portfolio.cash.items():
        if balances.get(currency, Decimal(0)) != amount:
            raise RuntimeError(f"cash reconciliation failed for {currency}")
    rows = session.scalars(
        select(PositionRecord).where(
            PositionRecord.account_id == account_id,
            PositionRecord.backtest_run_id.is_(None),
            PositionRecord.status == "OPEN",
        )
    ).all()
    stored = {}
    for row in rows:
        instrument = session.get(Instrument, row.instrument_id)
        if instrument is not None:
            stored[instrument.symbol] = row.quantity
    live = {symbol: position.quantity for symbol, position in portfolio.positions.items() if position.is_open}
    if stored != live:
        raise RuntimeError("position reconciliation failed")


def _features(session, instrument: Instrument, history: list[PriceBar], now: datetime) -> None:
    rows = compute_features(history, instrument.market, now=now)
    if not rows:
        return
    row = rows[-1]
    version = load_ai_config().feature_version
    existing = session.scalar(
        select(FeatureSnapshot).where(
            FeatureSnapshot.instrument_id == instrument.id,
            FeatureSnapshot.timestamp == row.timestamp,
            FeatureSnapshot.feature_version == version,
        )
    )
    values = {key: value for key, value in row.values.items()}
    if existing is None:
        session.add(
            FeatureSnapshot(
                instrument_id=instrument.id,
                timestamp=row.timestamp,
                available_at=row.available_at,
                feature_version=version,
                values=values,
            )
        )
    else:
        existing.values = values
    regime = RuleRegimeClassifier(load_ai_config()).classify(values)
    found = session.scalar(
        select(MarketRegime).where(
            MarketRegime.instrument_id == instrument.id,
            MarketRegime.timestamp == row.timestamp,
            MarketRegime.method == "rule",
        )
    )
    if found is None:
        session.add(
            MarketRegime(
                instrument_id=instrument.id,
                timestamp=row.timestamp,
                available_at=row.available_at,
                regime=regime,
                method="rule",
                details={},
            )
        )


def _accept_bars(session, bars: list[NormalizedBar], now: datetime) -> list[NormalizedBar]:
    valid, _rejected = partition_bars(bars)
    closed, _dropped = drop_open_bars(valid, now)
    for bar in closed:
        instrument = ensure_instrument(session, bar.market, bar.symbol, currency=bar.market == "JP" and "JPY" or "USD")
        exists = session.scalar(
            select(MarketBar.id).where(
                MarketBar.instrument_id == instrument.id,
                MarketBar.timeframe == bar.timeframe,
                MarketBar.timestamp == bar.timestamp,
                MarketBar.provider == bar.provider,
            )
        )
        if exists is None:
            session.add(
                MarketBar(
                    instrument_id=instrument.id,
                    timeframe=bar.timeframe,
                    timestamp=bar.timestamp,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    provider=bar.provider,
                    is_adjusted=bar.is_adjusted,
                )
            )
    session.flush()
    return closed


def _history(session, instrument_id: int, bar_start: datetime) -> list[PriceBar]:
    rows = load_bars(session, instrument_id, "5m", end=bar_start + timedelta(minutes=5))
    rows = [row for row in rows if row.timestamp <= bar_start]
    latest: dict[datetime, MarketBar] = {}
    for row in rows:
        latest[row.timestamp] = row
    ordered = [latest[key] for key in sorted(latest)]
    instrument = session.get(Instrument, instrument_id)
    symbol = instrument.symbol if instrument is not None else ""
    return [
        PriceBar(symbol, row.timestamp, row.open, row.high, row.low, row.close, row.volume)
        for row in ordered
    ]


def _snapshot(session, account_id: int, portfolio: Portfolio, broker: SimulatedBroker, timestamp: datetime) -> None:
    for currency in sorted(portfolio.cash):
        equity = portfolio.equity(currency)
        peak = _peak(session, account_id, currency, equity)
        drawdown = None if equity is None or peak is None or peak == 0 else (peak - equity) / peak
        session.add(
            PortfolioSnapshot(
                account_id=account_id,
                timestamp=timestamp,
                currency=currency,
                cash=portfolio.cash.get(currency, Decimal(0)),
                market_value=portfolio.market_value(currency),
                equity=equity,
                drawdown=drawdown,
            )
        )


def _peak(session, account_id, currency, equity) -> Decimal | None:
    if equity is None:
        return None
    previous = session.scalars(
        select(PortfolioSnapshot.equity).where(
            PortfolioSnapshot.account_id == account_id,
            PortfolioSnapshot.currency == currency,
            PortfolioSnapshot.equity.is_not(None),
        )
    ).all()
    values = [item for item in previous if item is not None]
    values.append(equity)
    return max(values) if values else None


def _instruments(session, account: Account) -> dict[str, Instrument]:
    found = {}
    for market in account.markets:
        if str(market).upper() == "JP":
            continue
        for symbol in account.symbols or []:
            instrument = get_instrument(session, str(market).upper(), str(symbol).upper())
            if instrument is None:
                instrument = ensure_instrument(session, str(market).upper(), str(symbol).upper())
            found[instrument.symbol] = instrument
    return found


def _symbols_for(account, market, instruments) -> list[str]:
    ordered = []
    for symbol in account.symbols or []:
        instrument = instruments.get(str(symbol).upper())
        if instrument is not None and instrument.market == market and instrument.symbol not in ordered:
            ordered.append(instrument.symbol)
    return ordered


def _last_checkpoint(session, account_id, market, symbol) -> datetime | None:
    return session.scalar(
        select(ProcessingCheckpoint.bar_timestamp)
        .where(
            ProcessingCheckpoint.account_id == account_id,
            ProcessingCheckpoint.market == market,
            ProcessingCheckpoint.symbol == symbol,
        )
        .order_by(ProcessingCheckpoint.bar_timestamp.desc())
        .limit(1)
    )


def _mark_checkpoint(session, account_id, market, symbol, bar_timestamp) -> None:
    exists = session.scalar(
        select(ProcessingCheckpoint.id).where(
            ProcessingCheckpoint.account_id == account_id,
            ProcessingCheckpoint.market == market,
            ProcessingCheckpoint.symbol == symbol,
            ProcessingCheckpoint.bar_timestamp == bar_timestamp,
        )
    )
    if exists is None:
        session.add(
            ProcessingCheckpoint(account_id=account_id, market=market, symbol=symbol, bar_timestamp=bar_timestamp)
        )


def _lock_job(session, now: datetime, job_name: str) -> SchedulerJob | None:
    job = session.scalar(select(SchedulerJob).where(SchedulerJob.name == job_name).with_for_update())
    if job is None:
        job = SchedulerJob(name=job_name, status="IDLE")
        session.add(job)
        session.flush()
    wall = datetime.now(timezone.utc)
    if job.status == "RUNNING" and job.heartbeat_at is not None and wall - _aware(job.heartbeat_at) < STALE_LOCK:
        return None
    job.status = "RUNNING"
    job.locked_by = "runtime"
    job.heartbeat_at = wall
    session.flush()
    return job


def _ensure_session(session, account, now: datetime) -> None:
    trading = latest_session(session, account.id)
    if trading is None:
        session.add(
            TradingSession(
                account_id=account.id,
                mode=account.mode,
                status="PAUSED" if account.mode == "PAUSED" else "RUNNING",
                resume_mode=account.mode if account.mode != "PAUSED" else "RULE_BASED",
                fill_model=FILL_MODEL,
                started_at=now,
                updated_at=now,
            )
        )
        session.flush()


def _notify(session, account_id: int | None, level: str, code: str, message: str) -> None:
    if level == "INFO":
        cutoff = datetime.now(timezone.utc) - timedelta(hours=6)
        existing = session.scalar(
            select(NotificationEvent.id).where(
                NotificationEvent.account_id == account_id,
                NotificationEvent.code == code,
                NotificationEvent.created_at >= cutoff,
            )
        )
        if existing is not None:
            return
    session.add(NotificationEvent(account_id=account_id, level=level, code=code, message=message))


def _record_failure(account_name: str, message: str, job_name: str = JOB_NAME) -> None:
    from app.db.session import SessionLocal

    try:
        with SessionLocal() as session:
            account = session.scalar(select(Account).where(Account.name == account_name))
            job = session.scalar(select(SchedulerJob).where(SchedulerJob.name == job_name))
            if job is not None:
                job.status = "FAILED"
                job.last_error = message
                job.heartbeat_at = datetime.now(timezone.utc)
            session.add(
                NotificationEvent(
                    account_id=None if account is None else account.id,
                    level="ERROR",
                    code="cycle_failed",
                    message=message,
                )
            )
            session.commit()
    except Exception:
        return


def _execution_exists(session, idempotency_key: str, timestamp: datetime) -> bool:
    order = session.scalar(select(OrderRecord).where(OrderRecord.idempotency_key == idempotency_key))
    if order is None:
        return False
    return (
        session.scalar(
            select(ExecutionRecord.id).where(ExecutionRecord.order_id == order.id, ExecutionRecord.executed_at == timestamp)
        )
        is not None
    )


def _pending_buy(broker: SimulatedBroker, symbol: str) -> bool:
    return any(order.symbol == symbol and order.side == "BUY" and order.status in OPEN_ORDER_STATUSES for order in broker.orders.values())


def _open_position(portfolio: Portfolio, symbol: str) -> bool:
    position = portfolio.positions.get(symbol)
    return position is not None and position.is_open


def _order_key(account_id: int, symbol: str, timestamp: datetime, side: str) -> str:
    return f"paper:{account_id}:{symbol}:{timestamp.isoformat()}:{side}"


def _currency(session, symbol: str, market: str) -> str:
    instrument = get_instrument(session, market, symbol)
    if instrument is not None:
        return instrument.currency
    return "JPY" if market == "JP" else "USD"


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
