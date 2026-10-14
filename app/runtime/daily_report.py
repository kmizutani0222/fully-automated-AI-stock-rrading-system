from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.calendar import zone_for
from app.models import Instrument
from app.models.ai import StrategyDecision
from app.models.runtime import JobExecution, MarketDataHealth, ProcessingCheckpoint, SchedulerJob
from app.models.trading import Account, ExecutionRecord, OrderRecord, PositionRecord, StrategySignalRecord

REJECTION_REASONS = {"invalid_stop", "INSUFFICIENT_CASH", "DAILY_LOSS", "HALTED", "MAX_POSITIONS", "INVALID_PRICE"}


def paper_daily_report(session: Session, account: Account, day: date | None = None) -> dict:
    zone = zone_for("US")
    if day is None:
        latest = session.scalar(
            select(func.max(ProcessingCheckpoint.bar_timestamp)).where(ProcessingCheckpoint.account_id == account.id)
        )
        day = datetime.now(timezone.utc).astimezone(zone).date() if latest is None else latest.astimezone(zone).date()
    start = datetime.combine(day, time.min, tzinfo=zone)
    end = start + timedelta(days=1)
    checkpoints = session.scalars(
        select(ProcessingCheckpoint.bar_timestamp).where(
            ProcessingCheckpoint.account_id == account.id,
            ProcessingCheckpoint.bar_timestamp >= start,
            ProcessingCheckpoint.bar_timestamp < end,
        )
    ).all()
    instrument_ids = [
        row.id
        for row in session.scalars(select(Instrument).where(Instrument.symbol.in_(account.symbols or []), Instrument.market == "US")).all()
    ]
    signals = 0
    rejections = 0
    if instrument_ids and checkpoints:
        signals = session.scalar(
            select(func.count()).select_from(StrategySignalRecord).where(
                StrategySignalRecord.backtest_run_id.is_(None),
                StrategySignalRecord.instrument_id.in_(instrument_ids),
                StrategySignalRecord.timestamp.in_(checkpoints),
            )
        ) or 0
        rejections = session.scalar(
            select(func.count()).select_from(StrategyDecision).where(
                StrategyDecision.backtest_run_id.is_(None),
                StrategyDecision.instrument_id.in_(instrument_ids),
                StrategyDecision.timestamp.in_(checkpoints),
                StrategyDecision.reason.in_(REJECTION_REASONS),
            )
        ) or 0
    orders = session.scalar(
        select(func.count()).select_from(OrderRecord).where(
            OrderRecord.account_id == account.id,
            OrderRecord.created_at >= start,
            OrderRecord.created_at < end,
        )
    ) or 0
    fills = session.scalar(
        select(func.count())
        .select_from(ExecutionRecord)
        .join(OrderRecord, OrderRecord.id == ExecutionRecord.order_id)
        .where(
            OrderRecord.account_id == account.id,
            ExecutionRecord.executed_at >= start,
            ExecutionRecord.executed_at < end,
        )
    ) or 0
    closes = session.scalar(
        select(func.count()).select_from(PositionRecord).where(
            PositionRecord.account_id == account.id,
            PositionRecord.status == "CLOSED",
            PositionRecord.closed_at >= start,
            PositionRecord.closed_at < end,
        )
    ) or 0
    failures = 0
    if account.symbols:
        failures = session.scalar(
            select(func.count()).select_from(MarketDataHealth).where(
                MarketDataHealth.symbol.in_([str(symbol).upper() for symbol in account.symbols]),
                MarketDataHealth.status == "failed",
                MarketDataHealth.fetched_at >= start,
                MarketDataHealth.fetched_at < end,
            )
        ) or 0
    scheduler_failures = session.scalar(
        select(func.count())
        .select_from(JobExecution)
        .join(SchedulerJob, SchedulerJob.id == JobExecution.job_id)
        .where(
            SchedulerJob.name == account.name,
            JobExecution.status == "FAILED",
            JobExecution.started_at >= start,
            JobExecution.started_at < end,
        )
    ) or 0
    realized = None
    cost = None
    if closes:
        realized = session.scalar(
            select(func.coalesce(func.sum(PositionRecord.realized_pnl), 0)).where(
                PositionRecord.account_id == account.id,
                PositionRecord.status == "CLOSED",
                PositionRecord.closed_at >= start,
                PositionRecord.closed_at < end,
            )
        )
    if fills:
        cost = session.scalar(
            select(func.coalesce(func.sum(ExecutionRecord.commission + ExecutionRecord.slippage_cost + ExecutionRecord.spread_cost), 0))
            .select_from(ExecutionRecord)
            .join(OrderRecord, OrderRecord.id == ExecutionRecord.order_id)
            .where(
                OrderRecord.account_id == account.id,
                ExecutionRecord.executed_at >= start,
                ExecutionRecord.executed_at < end,
            )
        )
    return {
        "account": account.name,
        "role": "operational" if account.name == "paper" else "verification",
        "day": day.isoformat(),
        "bars_processed": len(checkpoints),
        "signals": int(signals),
        "orders": int(orders),
        "fills": int(fills),
        "closes": int(closes),
        "rejections": int(rejections),
        "realized_pnl": _text(realized),
        "unrealized_pnl": None,
        "cost_total": _text(cost),
        "data_failures": int(failures),
        "scheduler_failures": int(scheduler_failures),
        "profitability_evaluated": False,
        "profitability_note": "注文がないため収益性は評価しません" if int(orders) == 0 else "このレポートは収益性の判定をしません",
    }


def _text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")
