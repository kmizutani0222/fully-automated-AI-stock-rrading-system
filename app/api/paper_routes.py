from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import assert_phase1_safety, get_settings
from app.db.session import get_session
from app.models import Instrument
from app.models.ai import StrategyDecision
from app.models.runtime import AccountModeChange, JobExecution, MarketDataHealth, ProcessingCheckpoint, SchedulerJob
from app.models.trading import Account, CashLedgerEntry, ExecutionRecord, OrderRecord, PositionRecord, StrategySignalRecord
from app.runtime.accounts import cash_balances, post_cash
from app.runtime.auth import require_admin, write_audit
from app.runtime.daily_report import paper_daily_report
from app.runtime.marketdata import gap_rows
from app.runtime.performance import performance_report
from app.trading.ema_diagnostic import ema_diagnostic
from app.trading.strategies import strategy_names

router = APIRouter(prefix="/api/v1")


class DepositIn(BaseModel):
    account_id: int | None = None
    currency: str
    amount: Decimal = Field(gt=0)
    market: str = "US"


class StrategyIn(BaseModel):
    account_id: int | None = None
    strategies: list[str]
    symbol_strategies: dict[str, str] = Field(default_factory=dict)


def _account(session: Session, account_id: int | None) -> Account:
    if account_id is None:
        account = session.scalar(select(Account).where(Account.name == "paper"))
    else:
        account = session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    return account


def _text(value) -> str | None:
    if value is None:
        return None
    return format(value, "f") if isinstance(value, Decimal) else value.isoformat() if hasattr(value, "isoformat") else str(value)


@router.get("/paper/status")
def paper_status(account_id: int | None = None, db: Session = Depends(get_session)) -> dict:
    account = _account(db, account_id)
    checkpoint = db.scalar(
        select(ProcessingCheckpoint)
        .where(ProcessingCheckpoint.account_id == account.id)
        .order_by(ProcessingCheckpoint.bar_timestamp.desc())
    )
    instrument_ids = [row.id for row in db.scalars(select(Instrument).where(Instrument.symbol.in_(account.symbols or []))).all()]
    processed_times = select(ProcessingCheckpoint.bar_timestamp).where(ProcessingCheckpoint.account_id == account.id)
    decision = None
    signal = None
    if instrument_ids:
        decision = db.scalar(
            select(StrategyDecision)
            .where(
                StrategyDecision.instrument_id.in_(instrument_ids),
                StrategyDecision.backtest_run_id.is_(None),
                StrategyDecision.timestamp.in_(processed_times),
            )
            .order_by(StrategyDecision.id.desc())
        )
        signal = db.scalar(
            select(StrategySignalRecord)
            .where(
                StrategySignalRecord.instrument_id.in_(instrument_ids),
                StrategySignalRecord.backtest_run_id.is_(None),
                StrategySignalRecord.timestamp.in_(processed_times),
            )
            .order_by(StrategySignalRecord.id.desc())
        )
    order = db.scalar(select(OrderRecord).where(OrderRecord.account_id == account.id).order_by(OrderRecord.id.desc()))
    execution = None
    if order is not None:
        execution = db.scalar(select(ExecutionRecord).where(ExecutionRecord.order_id == order.id).order_by(ExecutionRecord.id.desc()))
    rejected = db.scalar(
        select(OrderRecord)
        .where(OrderRecord.account_id == account.id, OrderRecord.status == "REJECTED")
        .order_by(OrderRecord.id.desc())
    )
    return {
        "account_id": account.id,
        "mode": account.mode,
        "cash": {key: format(value, "f") for key, value in cash_balances(db, account.id).items()},
        "symbols": account.symbols,
        "strategies": account.strategies,
        "symbol_strategies": account.symbol_strategies or {},
        "last_bar": None if checkpoint is None else checkpoint.bar_timestamp,
        "latest_signal": None
        if signal is None
        else {"signal": signal.signal, "reason": signal.reason, "timestamp": signal.timestamp, "strategy": signal.strategy_name},
        "latest_decision": None if decision is None else {"choice": decision.choice, "reason": decision.reason, "timestamp": decision.timestamp},
        "latest_order": None
        if order is None
        else {
            "id": order.id,
            "side": order.side,
            "quantity": _text(order.quantity),
            "status": order.status,
            "submitted_at": order.submitted_at,
            "reason": order.decision_reason,
        },
        "latest_fill": None
        if execution is None
        else {
            "price": _text(execution.price),
            "quantity": _text(execution.quantity),
            "filled_at": execution.filled_at or execution.fill_observed_at,
            "submitted_at": None if order is None else order.submitted_at,
        },
        "rejection_reason": None if decision is None or decision.choice in {"DAY", "RULE"} else decision.reason,
        "rejected_order_reason": None if rejected is None else rejected.decision_reason,
        **_audit_summary(db, account, signal, order, execution),
    }


def _audit_summary(db: Session, account: Account, signal, order, execution) -> dict:
    order_count = db.scalar(select(func.count()).select_from(OrderRecord).where(OrderRecord.account_id == account.id)) or 0
    fill_count = db.scalar(
        select(func.count())
        .select_from(ExecutionRecord)
        .join(OrderRecord, OrderRecord.id == ExecutionRecord.order_id)
        .where(OrderRecord.account_id == account.id)
    ) or 0
    close_count = db.scalar(
        select(func.count()).select_from(PositionRecord).where(
            PositionRecord.account_id == account.id,
            PositionRecord.backtest_run_id.is_(None),
            PositionRecord.status == "CLOSED",
        )
    ) or 0
    changes = db.scalars(
        select(AccountModeChange).where(AccountModeChange.account_id == account.id).order_by(AccountModeChange.id.desc()).limit(20)
    ).all()
    health = []
    for symbol in account.symbols or []:
        row = db.scalar(
            select(MarketDataHealth)
            .where(MarketDataHealth.market == "US", MarketDataHealth.symbol == str(symbol).upper())
            .order_by(MarketDataHealth.id.desc())
        )
        health.append(
            {
                "symbol": str(symbol).upper(),
                "status": None if row is None else row.status,
                "fetched_at": None if row is None else row.fetched_at,
                "failure_count": None if row is None else row.failure_count,
                "message": None if row is None else row.message,
            }
        )
    statuses = [item["status"] for item in health if item["status"]]
    if any(item == "failed" for item in statuses):
        data_status = "failed"
    elif statuses and all(item == "ok" for item in statuses):
        data_status = "ok"
    elif statuses:
        data_status = statuses[0]
    else:
        data_status = "unknown"
    return {
        "account_name": account.name,
        "account_role": "operational" if account.name == "paper" else "verification",
        "last_signal_at": None if signal is None else signal.timestamp,
        "last_order_at": None if order is None else order.submitted_at or order.created_at,
        "last_fill_at": None if execution is None else execution.filled_at or execution.fill_observed_at,
        "order_count": int(order_count),
        "fill_count": int(fill_count),
        "close_count": int(close_count),
        "data_status": data_status,
        "data_health": health,
        "mode_changes": [
            {
                "previous_mode": row.previous_mode,
                "new_mode": row.new_mode,
                "changed_at": row.changed_at,
                "actor": row.actor,
                "action": row.action,
                "request_id": row.request_id,
                "reason": row.reason,
            }
            for row in changes
        ],
    }


@router.get("/paper/ema-diagnostic")
def paper_ema_diagnostic(symbol: str = "AAPL", limit: int = 100, db: Session = Depends(get_session)) -> dict:
    return ema_diagnostic(db, symbol=symbol.upper(), limit=limit)


@router.get("/paper/daily-report")
def daily_report(day: date | None = None, db: Session = Depends(get_session)) -> dict:
    account = db.scalar(select(Account).where(Account.name == "paper"))
    if account is None:
        raise HTTPException(status_code=404, detail="operational paper account is missing")
    return paper_daily_report(db, account, day)


@router.post("/paper/deposits")
def paper_deposit(
    payload: DepositIn,
    actor: str = Depends(require_admin),
    db: Session = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key is required")
    account = _account(db, payload.account_id)
    before = cash_balances(db, account.id)
    try:
        entry = post_cash(db, account, payload.currency.upper(), payload.amount, "DEPOSIT", idempotency_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    write_audit(
        db,
        actor,
        "paper_deposit",
        "/api/v1/paper/deposits",
        {"account_id": account.id, "currency": payload.currency.upper(), "amount": format(payload.amount, "f"), "idempotency_key": idempotency_key},
    )
    db.commit()
    after = cash_balances(db, account.id)
    return {
        "ledger_id": entry.id,
        "account_id": account.id,
        "currency": entry.currency,
        "amount": format(entry.amount, "f"),
        "balance_after": format(entry.balance_after, "f"),
        "jpy_unchanged": before.get("JPY") == after.get("JPY"),
        "cash": {key: format(value, "f") for key, value in after.items()},
    }


@router.post("/paper/strategies")
def paper_strategies(
    payload: StrategyIn,
    actor: str = Depends(require_admin),
    db: Session = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key is required")
    from app.models.runtime import AuditLog

    seen = db.scalars(select(AuditLog).where(AuditLog.action == "paper_strategies").order_by(AuditLog.id.desc()).limit(100)).all()
    account = _account(db, payload.account_id)
    if any((row.details or {}).get("key") == idempotency_key for row in seen):
        return {"account_id": account.id, "strategies": account.strategies, "symbol_strategies": account.symbol_strategies or {}}
    known = set(strategy_names())
    if any(name not in known for name in payload.strategies) or any(name not in known for name in payload.symbol_strategies.values()):
        raise HTTPException(status_code=400, detail="unknown strategy")
    account.strategies = payload.strategies
    account.symbol_strategies = payload.symbol_strategies
    write_audit(db, actor, "paper_strategies", "/api/v1/paper/strategies", {"account_id": account.id, "strategies": payload.strategies, "key": idempotency_key})
    db.commit()
    return {"account_id": account.id, "strategies": account.strategies, "symbol_strategies": account.symbol_strategies}


@router.get("/paper/executions")
def paper_executions(account_id: int | None = None, db: Session = Depends(get_session)) -> list[dict]:
    account = _account(db, account_id)
    orders = db.scalars(select(OrderRecord).where(OrderRecord.account_id == account.id).order_by(OrderRecord.id.desc())).all()
    rows = []
    for order in orders[:100]:
        execution = db.scalar(select(ExecutionRecord).where(ExecutionRecord.order_id == order.id).order_by(ExecutionRecord.id.desc()))
        observed = None if execution is None else execution.filled_at or execution.fill_observed_at
        duration = None
        if observed is not None and order.submitted_at is not None:
            duration = (observed - order.submitted_at).total_seconds()
        rows.append(
            {
                "order_id": order.id,
                "side": order.side,
                "status": order.status,
                "quantity": _text(order.quantity),
                "strategy": order.strategy_name,
                "reason": order.decision_reason,
                "bar_close_time": order.bar_close_time,
                "data_received_at": order.data_received_at,
                "signal_generated_at": order.signal_generated_at,
                "order_created_at": order.created_at,
                "order_submitted_at": order.submitted_at,
                "fill_observed_at": None if execution is None else execution.fill_observed_at,
                "filled_at": observed,
                "fill_price": None if execution is None else _text(execution.price),
                "fill_quantity": None if execution is None else _text(execution.quantity),
                "seconds_to_fill": duration,
            }
        )
    return rows


@router.get("/paper/performance")
def paper_performance(account_id: int | None = None, db: Session = Depends(get_session)) -> dict:
    account = _account(db, account_id)
    return {"account_id": account.id, **performance_report(db, account.id)}


@router.get("/market-data/health")
def market_health(db: Session = Depends(get_session)) -> list[dict]:
    rows = db.scalars(select(MarketDataHealth).order_by(MarketDataHealth.id.desc()).limit(50)).all()
    return [
        {
            "market": row.market,
            "symbol": row.symbol,
            "provider": row.provider,
            "status": row.status,
            "fetched_at": row.fetched_at,
            "latest_bar_at": row.latest_bar_at,
            "lag_seconds": row.lag_seconds,
            "gap_count": row.gap_count,
            "failure_count": row.failure_count,
            "message": row.message,
        }
        for row in rows
    ]


@router.get("/market-data/gaps")
def market_gaps(market: str = "US", symbol: str = "AAPL", db: Session = Depends(get_session)) -> dict:
    missing = gap_rows(db, market.upper(), symbol.upper())
    return {"market": market.upper(), "symbol": symbol.upper(), "gaps": missing, "gap_count": len(missing)}


@router.get("/runtime/diagnostics")
def runtime_diagnostics(db: Session = Depends(get_session)) -> dict:
    assert_phase1_safety(get_settings())
    account = db.scalar(select(Account).where(Account.name == "paper"))
    job = db.scalar(select(SchedulerJob).where(SchedulerJob.name == "paper"))
    last_job = None if job is None else db.scalar(select(JobExecution).where(JobExecution.job_id == job.id).order_by(JobExecution.id.desc()))
    health = db.scalar(select(MarketDataHealth).order_by(MarketDataHealth.id.desc()))
    return {
        "trading_mode": get_settings().trading_mode,
        "live_trading_enabled": False,
        "account_mode": None if account is None else account.mode,
        "scheduler": None if job is None else job.status,
        "last_job_status": None if last_job is None else last_job.status,
        "last_job_reason": None if last_job is None else last_job.reason,
        "last_fetch_status": None if health is None else health.status,
        "last_fetch_at": None if health is None else health.fetched_at,
        "provider": None if health is None else health.provider,
    }
