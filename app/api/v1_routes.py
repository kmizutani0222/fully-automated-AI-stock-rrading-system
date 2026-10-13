from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.backtest.service import execute_backtest_run, list_runs, paper_cash, queue_backtest
from app.db.session import get_session
from app.models.trading import BacktestRun, BacktestTrade, OrderRecord, PortfolioSnapshot, RiskEvent, StrategyRecord
from app.portfolio.ledger import Portfolio
from app.schemas.trading import BacktestIn, BacktestOut
from app.trading.config import load_trading_config
from app.trading.strategies import strategy_names

router = APIRouter(prefix="/api/v1")


def _out(run: BacktestRun) -> BacktestOut:
    return BacktestOut(
        id=run.id,
        status=run.status,
        strategy=run.strategy_name,
        market=run.market,
        symbols=list(run.symbols),
        metrics=run.metrics,
        error=run.error,
    )


@router.get("/strategies")
def strategies(db: Session = Depends(get_session)) -> list[dict]:
    rows = list(db.scalars(select(StrategyRecord).order_by(StrategyRecord.name)).all())
    if rows:
        return [{"name": row.name, "version": row.version, "parameters": row.parameters} for row in rows]
    config = load_trading_config()
    return [{"name": name, "version": config.strategy_version, "parameters": {}} for name in strategy_names()]


@router.post("/backtests", status_code=202)
def create_backtest(payload: BacktestIn, background: BackgroundTasks, db: Session = Depends(get_session)) -> dict:
    try:
        run = queue_backtest(
            db,
            strategy_name=payload.strategy,
            market=payload.market,
            symbols=payload.symbols,
            start=payload.start,
            end=payload.end,
            provider=payload.provider,
            initial_cash=payload.initial_cash,
            parameters=payload.parameters,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    background.add_task(execute_backtest_run, run.id)
    return {"id": run.id, "status": run.status}


@router.get("/backtests", response_model=list[BacktestOut])
def backtests(db: Session = Depends(get_session)) -> list[BacktestOut]:
    return [_out(run) for run in list_runs(db)]


@router.get("/backtests/{run_id}", response_model=BacktestOut)
def backtest_detail(run_id: int, db: Session = Depends(get_session)) -> BacktestOut:
    run = db.get(BacktestRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="backtest not found")
    return _out(run)


@router.get("/backtests/{run_id}/trades")
def backtest_trades(run_id: int, db: Session = Depends(get_session)) -> list[dict]:
    rows = db.scalars(select(BacktestTrade).where(BacktestTrade.backtest_run_id == run_id).order_by(BacktestTrade.fill_time)).all()
    return [
        {
            "side": row.side,
            "quantity": format(row.quantity, "f"),
            "signal_time": row.signal_time,
            "fill_time": row.fill_time,
            "fill_price": format(row.fill_price, "f"),
            "commission": format(row.commission, "f"),
            "slippage_cost": format(row.slippage_cost, "f"),
            "realized_pnl": None if row.realized_pnl is None else format(row.realized_pnl, "f"),
        }
        for row in rows
    ]


@router.get("/backtests/{run_id}/equity")
def backtest_equity(run_id: int, db: Session = Depends(get_session)) -> list[dict]:
    rows = db.scalars(
        select(PortfolioSnapshot).where(PortfolioSnapshot.backtest_run_id == run_id).order_by(PortfolioSnapshot.timestamp)
    ).all()
    return [
        {
            "timestamp": row.timestamp,
            "currency": row.currency,
            "cash": format(row.cash, "f"),
            "market_value": format(row.market_value, "f"),
            "equity": None if row.equity is None else format(row.equity, "f"),
            "drawdown": None if row.drawdown is None else format(row.drawdown, "f"),
        }
        for row in rows
    ]


@router.get("/portfolio")
def portfolio(db: Session = Depends(get_session)) -> dict:
    latest: dict[str, Decimal] = {}
    for entry in paper_cash(db):
        latest[entry.currency] = entry.balance_after
    if not latest:
        latest = {key: value for key, value in load_trading_config().cash().items()}
    book = Portfolio({key: Decimal(value) for key, value in latest.items()})
    equity = {key: format(value, "f") for key, value in latest.items()}
    reporting = book.reporting_equity("JPY", {})
    return {
        "cash": {key: format(value, "f") for key, value in latest.items()},
        "reserved_cash": {},
        "positions": [],
        "equity_by_currency": equity,
        "reporting_currency": "JPY",
        "equity": None if reporting is None else format(reporting, "f"),
        "realized_pnl": "0",
        "unrealized_pnl": "0",
    }


@router.get("/orders")
def orders(db: Session = Depends(get_session)) -> list[dict]:
    rows = db.scalars(select(OrderRecord).order_by(OrderRecord.id.desc()).limit(200)).all()
    return [
        {
            "id": row.id,
            "backtest_run_id": row.backtest_run_id,
            "side": row.side,
            "order_type": row.order_type,
            "quantity": format(row.quantity, "f"),
            "filled_quantity": format(row.filled_quantity, "f"),
            "status": row.status,
        }
        for row in rows
    ]


@router.get("/risk/events")
def risk_events(db: Session = Depends(get_session)) -> list[dict]:
    rows = db.scalars(select(RiskEvent).order_by(RiskEvent.id.desc()).limit(200)).all()
    return [{"id": row.id, "backtest_run_id": row.backtest_run_id, "code": row.code, "message": row.message} for row in rows]
