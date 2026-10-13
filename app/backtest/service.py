from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.backtest.engine import InsufficientDataError, run_backtest
from app.data.repository import get_instrument, load_bars
from app.db.session import SessionLocal
from app.models.trading import (
    BacktestRun,
    BacktestTrade,
    CashLedgerEntry,
    ExecutionRecord,
    OrderEvent,
    OrderRecord,
    PortfolioSnapshot,
    PositionRecord,
    RiskEvent,
    StrategySignalRecord,
)
from app.trading.config import TradingConfig, load_trading_config, with_overrides
from app.trading.strategies import PriceBar, build_strategy


def queue_backtest(
    session: Session,
    *,
    strategy_name: str,
    market: str,
    symbols: list[str],
    start: datetime,
    end: datetime,
    provider: str,
    initial_cash: dict[str, str] | None,
    parameters: dict | None,
) -> BacktestRun:
    config = load_trading_config()
    try:
        build_strategy(strategy_name, config)
    except KeyError as exc:
        raise ValueError(f"unknown strategy: {strategy_name}") from exc
    cash = initial_cash or {key: format(value, "f") for key, value in config.cash().items()}
    run = BacktestRun(
        strategy_name=strategy_name,
        strategy_version=config.strategy_version,
        status="PENDING",
        market=market.upper(),
        timeframe="5m",
        symbols=[symbol.upper() for symbol in symbols],
        start_at=start,
        end_at=end,
        provider=provider,
        parameters=parameters or {},
        initial_cash=cash,
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def execute_backtest_run(run_id: int) -> None:
    with SessionLocal() as session:
        run = session.get(BacktestRun, run_id)
        if run is None:
            return
        run.status = "RUNNING"
        run.started_at = datetime.now(timezone.utc)
        session.commit()
        try:
            output = _run(session, run)
            _persist(session, run, output)
            run.status = "SUCCEEDED"
            run.metrics = output.metrics
            run.finished_at = datetime.now(timezone.utc)
            run.error = None
            session.commit()
        except Exception as exc:  # noqa: BLE001 - persist the failure instead of leaving the job running
            session.rollback()
            failed = session.get(BacktestRun, run_id)
            if failed is not None:
                failed.status = "FAILED"
                failed.error = str(exc)
                failed.finished_at = datetime.now(timezone.utc)
                session.commit()
            return


def _run(session: Session, run: BacktestRun):
    config = with_overrides(load_trading_config(), run.parameters)
    strategy = build_strategy(run.strategy_name, config)
    bars: dict[str, list[PriceBar]] = {}
    currencies: dict[str, str] = {}
    for symbol in run.symbols:
        instrument = get_instrument(session, run.market, symbol)
        if instrument is None:
            raise InsufficientDataError(f"instrument {run.market}:{symbol} is not registered")
        rows = load_bars(session, instrument.id, run.timeframe, provider=run.provider, start=run.start_at, end=run.end_at)
        bars[symbol] = [
            PriceBar(
                symbol=symbol,
                timestamp=row.timestamp,
                open=Decimal(row.open),
                high=Decimal(row.high),
                low=Decimal(row.low),
                close=Decimal(row.close),
                volume=None if row.volume is None else Decimal(row.volume),
            )
            for row in rows
        ]
        currencies[symbol] = instrument.currency
    cash = {key: Decimal(str(value)) for key, value in run.initial_cash.items()}
    return run_backtest(bars, currencies, strategy, config, run.market, cash)


def _persist(session: Session, run: BacktestRun, output) -> None:
    instruments = {
        symbol: get_instrument(session, run.market, symbol)
        for symbol in run.symbols
    }
    order_ids: dict[int, int] = {}
    for order in output.orders:
        instrument = instruments[order.symbol]
        record = OrderRecord(
            backtest_run_id=run.id,
            instrument_id=instrument.id,
            side=order.side,
            order_type=order.order_type,
            quantity=order.quantity,
            filled_quantity=order.filled_quantity,
            limit_price=order.limit_price,
            status=order.status,
            idempotency_key=f"{run.id}:{order.idempotency_key}",
            created_at=order.created_at,
            updated_at=order.created_at,
        )
        session.add(record)
        session.flush()
        order_ids[order.id] = record.id
        for message in order.events:
            session.add(OrderEvent(order_id=record.id, status=order.status, message=message))
    for fill in output.fills:
        instrument = instruments[fill.symbol]
        session.add(
            ExecutionRecord(
                order_id=order_ids[fill.order_id],
                sequence=fill.sequence,
                quantity=fill.quantity,
                price=fill.price,
                commission=fill.commission,
                slippage_cost=fill.slippage_cost,
                spread_cost=fill.spread_cost,
                executed_at=fill.executed_at,
            )
        )
        session.add(
            BacktestTrade(
                backtest_run_id=run.id,
                instrument_id=instrument.id,
                side=fill.side,
                quantity=fill.quantity,
                signal_time=fill.signal_time,
                fill_time=fill.executed_at,
                reference_price=fill.reference_price,
                fill_price=fill.price,
                commission=fill.commission,
                slippage_cost=fill.slippage_cost,
                spread_cost=fill.spread_cost,
                realized_pnl=fill.realized_pnl,
            )
        )
    for signal in output.signals:
        session.add(
            StrategySignalRecord(
                strategy_name=run.strategy_name,
                instrument_id=instruments[signal.symbol].id,
                backtest_run_id=run.id,
                timestamp=signal.timestamp,
                signal=signal.side,
                reason=signal.reason,
            )
        )
    for point in output.equity:
        session.add(
            PortfolioSnapshot(
                backtest_run_id=run.id,
                timestamp=point.timestamp,
                currency=point.currency,
                cash=point.cash,
                market_value=point.market_value,
                equity=point.equity,
                drawdown=point.drawdown,
            )
        )
    for event in output.risk_events:
        session.add(
            RiskEvent(
                backtest_run_id=run.id,
                code=event["code"],
                message=event["message"],
                details=event.get("details") or {},
            )
        )
    for position in output.final_positions:
        instrument = instruments.get(position["symbol"])
        if instrument is None:
            continue
        session.add(
            PositionRecord(
                backtest_run_id=run.id,
                instrument_id=instrument.id,
                quantity=Decimal(position["quantity"]),
                average_price=Decimal(position["average_price"]),
                realized_pnl=Decimal(position["realized_pnl"]),
                status=position["status"],
            )
        )
    balances: dict[str, Decimal] = {key: Decimal(str(value)) for key, value in run.initial_cash.items()}
    for currency, amount in balances.items():
        session.add(
            CashLedgerEntry(
                backtest_run_id=run.id,
                currency=currency,
                amount=amount,
                balance_after=amount,
                reason="INITIAL",
            )
        )
    for entry in output.ledger:
        session.add(
            CashLedgerEntry(
                backtest_run_id=run.id,
                currency=entry.currency,
                amount=entry.amount,
                balance_after=entry.balance_after,
                reason=entry.reason,
            )
        )


def list_runs(session: Session) -> list[BacktestRun]:
    return list(session.scalars(select(BacktestRun).order_by(BacktestRun.id.desc())).all())


def paper_cash(session: Session) -> list[CashLedgerEntry]:
    return list(
        session.scalars(select(CashLedgerEntry).where(CashLedgerEntry.account_id.is_not(None)).order_by(CashLedgerEntry.id)).all()
    )
