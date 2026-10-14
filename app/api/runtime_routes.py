from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import assert_phase1_safety, get_settings
from app.db.session import get_session
from app.models import Instrument
from app.models.ai import ModelVersion, StrategyDecision
from app.models.runtime import AuditLog, JobExecution, NotificationEvent, SchedulerJob, TradingSession
from app.models.trading import Account, CashLedgerEntry, ExecutionRecord, OrderRecord, PortfolioSnapshot, PositionRecord
from app.runtime.accounts import MODES, cash_balances, create_account, latest_session, post_cash
from app.runtime.auth import require_admin, write_audit
from app.runtime.mode_history import record_mode_change
from app.runtime.schedule import FILL_MODEL
from app.schemas.runtime import AccountIn, AccountSettingsIn, CashIn, RuntimeCommand
from app.trading.strategies import strategy_names

router = APIRouter(prefix="/api/v1")


def _money(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def _account_out(session: Session, account: Account) -> dict:
    balances = cash_balances(session, account.id)
    return {
        "id": account.id,
        "name": account.name,
        "base_currency": account.base_currency,
        "markets": account.markets,
        "mode": account.mode,
        "strategies": account.strategies,
        "symbols": account.symbols,
        "fallback_to_rules": account.fallback_to_rules,
        "risk_settings": account.risk_settings,
        "cash": {key: format(value, "f") for key, value in balances.items()},
        "created_at": account.created_at,
    }


def _paper_account(session: Session, account_id: int | None) -> Account:
    if account_id is None:
        account = session.scalar(select(Account).where(Account.name == "paper"))
    else:
        account = session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    return account


@router.get("/runtime/status")
def runtime_status(db: Session = Depends(get_session)) -> dict:
    account = db.scalar(select(Account).where(Account.name == "paper"))
    job = db.scalar(select(SchedulerJob).where(SchedulerJob.name == "paper"))
    trading = None if account is None else latest_session(db, account.id)
    active_models = db.scalar(select(func.count()).select_from(ModelVersion).where(ModelVersion.status == "ACTIVE")) or 0
    return {
        "trading_mode": get_settings().trading_mode,
        "live_trading_enabled": False,
        "scheduler": None if job is None else job.status,
        "heartbeat_at": None if job is None else job.heartbeat_at,
        "account_mode": None if account is None else account.mode,
        "session_status": None if trading is None else trading.status,
        "active_models": int(active_models),
        "jp_auto_trading": False,
        "fill_model": FILL_MODEL,
        "autostart": get_settings().runtime_autostart,
    }


@router.post("/runtime/start")
def runtime_start(
    payload: RuntimeCommand,
    actor: str = Depends(require_admin),
    db: Session = Depends(get_session),
    request_id: str | None = Header(default=None, alias="X-Request-Id"),
) -> dict:
    assert_phase1_safety(get_settings())
    if payload.mode and payload.mode.upper() == "LIVE":
        raise HTTPException(status_code=400, detail="LIVE mode is not implemented")
    account = _paper_account(db, payload.account_id)
    previous_mode = account.mode
    mode = (payload.mode or account.mode or "RULE_BASED").upper()
    if mode == "PAUSED":
        mode = "RULE_BASED"
    if mode not in MODES:
        raise HTTPException(status_code=400, detail="unsupported mode")
    account.mode = mode
    record_mode_change(
        db,
        account_id=account.id,
        previous_mode=previous_mode,
        new_mode=mode,
        actor=actor,
        action="runtime_start",
        request_id=request_id,
        reason="operator_start",
    )
    trading = latest_session(db, account.id)
    now = datetime.now(timezone.utc)
    if trading is None:
        trading = TradingSession(
            account_id=account.id,
            mode=mode,
            status="RUNNING",
            resume_mode=mode,
            fill_model=FILL_MODEL,
            started_at=now,
            updated_at=now,
        )
        db.add(trading)
    else:
        trading.mode = mode
        trading.status = "RUNNING"
        trading.resume_mode = mode
        trading.updated_at = now
    write_audit(db, actor, "runtime_start", "/api/v1/runtime/start", {"account_id": account.id, "mode": mode})
    db.commit()
    return {"account_id": account.id, "mode": account.mode, "status": "RUNNING"}


@router.post("/runtime/pause")
def runtime_pause(
    payload: RuntimeCommand,
    actor: str = Depends(require_admin),
    db: Session = Depends(get_session),
    request_id: str | None = Header(default=None, alias="X-Request-Id"),
) -> dict:
    account = _paper_account(db, payload.account_id)
    now = datetime.now(timezone.utc)
    trading = latest_session(db, account.id)
    previous_mode = account.mode
    previous = account.mode if account.mode != "PAUSED" else (trading.resume_mode if trading else "RULE_BASED")
    account.mode = "PAUSED"
    record_mode_change(
        db,
        account_id=account.id,
        previous_mode=previous_mode,
        new_mode="PAUSED",
        actor=actor,
        action="runtime_pause",
        request_id=request_id,
        reason="operator_pause",
    )
    if trading is None:
        trading = TradingSession(
            account_id=account.id,
            mode="PAUSED",
            status="PAUSED",
            resume_mode=previous,
            fill_model=FILL_MODEL,
            started_at=now,
            updated_at=now,
        )
        db.add(trading)
    else:
        trading.mode = "PAUSED"
        trading.status = "PAUSED"
        trading.resume_mode = previous
        trading.updated_at = now
    write_audit(db, actor, "runtime_pause", "/api/v1/runtime/pause", {"account_id": account.id})
    from app.runtime.cycle import _notify

    _notify(db, account.id, "WARNING", "trading_halted", "新規買いは停止しました。保有の決済とリスク管理は続けます。")
    db.commit()
    return {"account_id": account.id, "mode": "PAUSED", "resume_mode": previous}


@router.post("/runtime/resume")
def runtime_resume(
    payload: RuntimeCommand,
    actor: str = Depends(require_admin),
    db: Session = Depends(get_session),
    request_id: str | None = Header(default=None, alias="X-Request-Id"),
) -> dict:
    assert_phase1_safety(get_settings())
    account = _paper_account(db, payload.account_id)
    trading = latest_session(db, account.id)
    previous_mode = account.mode
    mode = trading.resume_mode if trading and trading.resume_mode else "RULE_BASED"
    if mode == "PAUSED":
        mode = "RULE_BASED"
    account.mode = mode
    record_mode_change(
        db,
        account_id=account.id,
        previous_mode=previous_mode,
        new_mode=mode,
        actor=actor,
        action="runtime_resume",
        request_id=request_id,
        reason="operator_resume",
    )
    now = datetime.now(timezone.utc)
    if trading is None:
        trading = TradingSession(
            account_id=account.id,
            mode=mode,
            status="RUNNING",
            resume_mode=mode,
            fill_model=FILL_MODEL,
            started_at=now,
            updated_at=now,
        )
        db.add(trading)
    else:
        trading.mode = mode
        trading.status = "RUNNING"
        trading.updated_at = now
    write_audit(db, actor, "runtime_resume", "/api/v1/runtime/resume", {"account_id": account.id, "mode": mode})
    db.commit()
    return {"account_id": account.id, "mode": mode, "status": "RUNNING"}


@router.get("/runtime/jobs")
def runtime_jobs(db: Session = Depends(get_session)) -> list[dict]:
    rows = db.scalars(select(JobExecution).order_by(JobExecution.id.desc()).limit(100)).all()
    return [
        {
            "id": row.id,
            "status": row.status,
            "reason": row.reason,
            "bar_timestamp": row.bar_timestamp,
            "started_at": row.started_at,
            "finished_at": row.finished_at,
        }
        for row in rows
    ]


@router.get("/runtime/events")
def runtime_events(db: Session = Depends(get_session)) -> list[dict]:
    rows = db.scalars(select(NotificationEvent).order_by(NotificationEvent.id.desc()).limit(100)).all()
    return [{"id": row.id, "level": row.level, "code": row.code, "message": row.message, "created_at": row.created_at} for row in rows]


@router.get("/accounts")
def accounts(db: Session = Depends(get_session)) -> list[dict]:
    rows = db.scalars(select(Account).order_by(Account.id)).all()
    return [_account_out(db, row) for row in rows]


@router.post("/accounts", status_code=201)
def create_paper_account(payload: AccountIn, actor: str = Depends(require_admin), db: Session = Depends(get_session)) -> dict:
    unknown = [name for name in payload.strategies if name not in strategy_names()]
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown strategy: {unknown[0]}")
    if payload.mode.upper() == "LIVE":
        raise HTTPException(status_code=400, detail="LIVE mode is not implemented")
    try:
        account = create_account(
            db,
            name=payload.name,
            base_currency=payload.base_currency,
            jpy_cash=payload.jpy_cash,
            usd_cash=payload.usd_cash,
            markets=[item.upper() for item in payload.markets],
            strategies=payload.strategies,
            symbols=[item.upper() for item in payload.symbols],
            mode=payload.mode.upper(),
            fallback_to_rules=payload.fallback_to_rules,
            risk_settings=payload.risk_settings,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    write_audit(db, actor, "create_account", "/api/v1/accounts", {"name": account.name})
    db.commit()
    return _account_out(db, account)


@router.get("/accounts/{account_id}")
def account_detail(account_id: int, db: Session = Depends(get_session)) -> dict:
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    body = _account_out(db, account)
    positions = db.scalars(
        select(PositionRecord).where(PositionRecord.account_id == account.id, PositionRecord.backtest_run_id.is_(None), PositionRecord.status == "OPEN")
    ).all()
    body["positions"] = [
        {
            "instrument_id": row.instrument_id,
            "quantity": format(row.quantity, "f"),
            "average_price": format(row.average_price, "f"),
            "realized_pnl": format(row.realized_pnl, "f"),
        }
        for row in positions
    ]
    return body


@router.post("/accounts/{account_id}/settings")
def account_settings(
    account_id: int,
    payload: AccountSettingsIn,
    actor: str = Depends(require_admin),
    db: Session = Depends(get_session),
    request_id: str | None = Header(default=None, alias="X-Request-Id"),
) -> dict:
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    previous_mode = account.mode
    if payload.mode is not None:
        mode = payload.mode.upper()
        if mode == "LIVE" or mode not in MODES:
            raise HTTPException(status_code=400, detail="unsupported mode")
        account.mode = mode
        record_mode_change(
            db,
            account_id=account.id,
            previous_mode=previous_mode,
            new_mode=mode,
            actor=actor,
            action="update_account",
            request_id=request_id,
            reason="account_settings",
        )
    if payload.markets is not None:
        account.markets = [item.upper() for item in payload.markets]
    if payload.strategies is not None:
        account.strategies = payload.strategies
    if payload.symbols is not None:
        account.symbols = [item.upper() for item in payload.symbols]
    if payload.fallback_to_rules is not None:
        account.fallback_to_rules = payload.fallback_to_rules
    if payload.risk_settings is not None:
        account.risk_settings = payload.risk_settings
    write_audit(db, actor, "update_account", f"/api/v1/accounts/{account_id}/settings", payload.model_dump(exclude_none=True))
    db.commit()
    return _account_out(db, account)


@router.post("/accounts/{account_id}/cash")
def account_cash(account_id: int, payload: CashIn, actor: str = Depends(require_admin), db: Session = Depends(get_session)) -> dict:
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    try:
        entry = post_cash(db, account, payload.currency.upper(), payload.amount, payload.reason.upper())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    write_audit(db, actor, "cash_event", f"/api/v1/accounts/{account_id}/cash", {"currency": entry.currency, "amount": format(entry.amount, "f"), "reason": entry.reason})
    db.commit()
    return {"currency": entry.currency, "balance_after": format(entry.balance_after, "f"), "reason": entry.reason}


@router.post("/accounts/{account_id}/orders/{order_id}/cancel")
def cancel_order(account_id: int, order_id: int, actor: str = Depends(require_admin), db: Session = Depends(get_session)) -> dict:
    order = db.get(OrderRecord, order_id)
    if order is None or order.account_id != account_id:
        raise HTTPException(status_code=404, detail="order not found")
    if order.backtest_run_id is not None:
        raise HTTPException(status_code=400, detail="backtest orders are not paper orders")
    if order.status in {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}:
        return {"id": order.id, "status": order.status}
    order.status = "CANCELED"
    order.updated_at = datetime.now(timezone.utc)
    order.reserved_cash = Decimal(0)
    write_audit(db, actor, "cancel_order", f"/api/v1/accounts/{account_id}/orders/{order_id}/cancel", {"order_id": order_id})
    db.commit()
    return {"id": order.id, "status": order.status}


@router.get("/dashboard/summary")
def dashboard_summary(account_id: int | None = None, db: Session = Depends(get_session)) -> dict:
    account = _paper_account(db, account_id)
    balances = cash_balances(db, account.id)
    positions = db.scalars(
        select(PositionRecord).where(PositionRecord.account_id == account.id, PositionRecord.backtest_run_id.is_(None), PositionRecord.status == "OPEN")
    ).all()
    snapshots = db.scalars(
        select(PortfolioSnapshot)
        .where(PortfolioSnapshot.account_id == account.id, PortfolioSnapshot.backtest_run_id.is_(None))
        .order_by(PortfolioSnapshot.id.desc())
        .limit(20)
    ).all()
    latest_equity = {}
    for row in snapshots:
        latest_equity.setdefault(row.currency, row)
    drawdowns = [row.drawdown for row in snapshots if row.drawdown is not None]
    start_day = datetime.now(timezone.utc) - timedelta(days=1)
    day_rows = [
        row
        for row in db.scalars(
            select(PortfolioSnapshot).where(
                PortfolioSnapshot.account_id == account.id,
                PortfolioSnapshot.backtest_run_id.is_(None),
                PortfolioSnapshot.timestamp >= start_day,
            )
        ).all()
    ]
    day_pnl = None
    if day_rows:
        by_currency: dict[str, list] = {}
        for row in day_rows:
            if row.equity is None:
                continue
            by_currency.setdefault(row.currency, []).append(row)
        day_pnl = {}
        for currency, rows in by_currency.items():
            ordered = sorted(rows, key=lambda item: item.timestamp)
            day_pnl[currency] = format(ordered[-1].equity - ordered[0].equity, "f")
    realized = db.scalar(
        select(func.coalesce(func.sum(PositionRecord.realized_pnl), 0)).where(
            PositionRecord.account_id == account.id,
            PositionRecord.backtest_run_id.is_(None),
        )
    )
    decision = db.scalar(select(StrategyDecision).order_by(StrategyDecision.id.desc()).limit(1))
    orders = db.scalars(select(OrderRecord).where(OrderRecord.account_id == account.id).order_by(OrderRecord.id.desc()).limit(5)).all()
    job = db.scalar(select(SchedulerJob).where(SchedulerJob.name == "paper"))
    return {
        "account_id": account.id,
        "mode": account.mode,
        "scheduler": None if job is None else job.status,
        "cash": {key: format(value, "f") for key, value in balances.items()},
        "equity": {key: _money(row.equity) for key, row in latest_equity.items()},
        "day_pnl": day_pnl,
        "realized_pnl": _money(Decimal(realized or 0)),
        "max_drawdown": _money(max(drawdowns) if drawdowns else None),
        "open_positions": len(positions),
        "latest_decision": None
        if decision is None
        else {"choice": decision.choice, "reason": decision.reason, "timestamp": decision.timestamp},
        "latest_orders": [{"id": row.id, "side": row.side, "status": row.status, "quantity": format(row.quantity, "f")} for row in orders],
        "active_models": int(db.scalar(select(func.count()).select_from(ModelVersion).where(ModelVersion.status == "ACTIVE")) or 0),
        "fill_model": FILL_MODEL,
    }


@router.get("/dashboard/equity")
def dashboard_equity(account_id: int | None = None, db: Session = Depends(get_session)) -> list[dict]:
    account = _paper_account(db, account_id)
    rows = db.scalars(
        select(PortfolioSnapshot)
        .where(PortfolioSnapshot.account_id == account.id, PortfolioSnapshot.backtest_run_id.is_(None))
        .order_by(PortfolioSnapshot.timestamp)
    ).all()
    return [
        {
            "timestamp": row.timestamp,
            "currency": row.currency,
            "cash": format(row.cash, "f"),
            "market_value": format(row.market_value, "f"),
            "equity": _money(row.equity),
            "drawdown": _money(row.drawdown),
        }
        for row in rows[-500:]
    ]


@router.get("/dashboard/trades")
def dashboard_trades(account_id: int | None = None, db: Session = Depends(get_session)) -> list[dict]:
    account = _paper_account(db, account_id)
    rows = db.execute(
        select(ExecutionRecord, OrderRecord, Instrument)
        .join(OrderRecord, OrderRecord.id == ExecutionRecord.order_id)
        .join(Instrument, Instrument.id == OrderRecord.instrument_id)
        .where(OrderRecord.account_id == account.id, OrderRecord.backtest_run_id.is_(None))
        .order_by(ExecutionRecord.executed_at.desc())
        .limit(200)
    ).all()
    return [
        {
            "symbol": instrument.symbol,
            "side": order.side,
            "quantity": format(execution.quantity, "f"),
            "price": format(execution.price, "f"),
            "commission": format(execution.commission, "f"),
            "slippage_cost": format(execution.slippage_cost, "f"),
            "spread_cost": format(execution.spread_cost, "f"),
            "executed_at": execution.executed_at,
            "status": order.status,
        }
        for execution, order, instrument in rows
    ]


@router.get("/audit")
def audit(actor: str = Depends(require_admin), db: Session = Depends(get_session)) -> list[dict]:
    del actor
    rows = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(100)).all()
    return [{"id": row.id, "actor": row.actor, "action": row.action, "path": row.path, "created_at": row.created_at} for row in rows]
