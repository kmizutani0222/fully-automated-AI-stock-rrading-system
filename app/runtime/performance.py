from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.runtime import DailyPerformance
from app.models.trading import ExecutionRecord, OrderRecord, PortfolioSnapshot, PositionRecord


def day_change(opening: Decimal | None, closing: Decimal | None) -> Decimal | None:
    if opening is None or closing is None:
        return None
    return closing - opening


def refresh_daily(session: Session, account_id: int, currency: str, timestamp: datetime, equity: Decimal | None) -> None:
    if equity is None:
        return
    current = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
    day = current.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    session.flush()
    snapshots = session.scalars(
        select(PortfolioSnapshot)
        .where(
            PortfolioSnapshot.account_id == account_id,
            PortfolioSnapshot.currency == currency,
            PortfolioSnapshot.timestamp >= day,
            PortfolioSnapshot.timestamp < day + timedelta(days=1),
        )
        .order_by(PortfolioSnapshot.timestamp, PortfolioSnapshot.id)
    ).all()
    equities = [row.equity for row in snapshots if row.equity is not None]
    report = _metrics(session, account_id)
    values = {
        "equity": equities[-1] if equities else equity,
        "day_pnl": day_change(equities[0], equities[-1]) if len(equities) >= 2 else None,
        "max_drawdown": _drawdown(equities),
        "realized_pnl": report["realized_pnl"],
        "unrealized_pnl": report["unrealized_pnl"],
        "trade_count": report["trade_count"],
        "win_rate": report["win_rate"],
        "cost_total": report["cost_total"],
    }
    row = session.scalar(
        select(DailyPerformance).where(
            DailyPerformance.account_id == account_id,
            DailyPerformance.trading_day == day,
            DailyPerformance.currency == currency,
        )
    )
    if row is None:
        session.add(DailyPerformance(account_id=account_id, trading_day=day, currency=currency, **values))
        return
    for key, value in values.items():
        setattr(row, key, value)


def performance_report(session: Session, account_id: int) -> dict:
    snapshots = session.scalars(
        select(PortfolioSnapshot).where(PortfolioSnapshot.account_id == account_id).order_by(PortfolioSnapshot.timestamp, PortfolioSnapshot.id)
    ).all()
    grouped: dict[str, list[PortfolioSnapshot]] = {}
    for row in snapshots:
        grouped.setdefault(row.currency, []).append(row)
    metrics = _metrics(session, account_id)
    currencies = {}
    for currency, rows in grouped.items():
        equities = [row.equity for row in rows if row.equity is not None]
        latest = rows[-1]
        currencies[currency] = {
            "equity": _text(latest.equity),
            "cash": _text(latest.cash),
            "market_value": _text(latest.market_value),
            "day_pnl": _text(day_change(equities[0], equities[-1]) if len(equities) >= 2 else None),
            "max_drawdown": _text(_drawdown(equities)),
        }
    return {
        "realized_pnl": _text(metrics["realized_pnl"]),
        "unrealized_pnl": _text(metrics["unrealized_pnl"]),
        "trade_count": metrics["trade_count"],
        "win_rate": _text(metrics["win_rate"]),
        "cost_total": _text(metrics["cost_total"]),
        "currencies": currencies,
    }


def _metrics(session: Session, account_id: int) -> dict:
    positions = session.scalars(select(PositionRecord).where(PositionRecord.account_id == account_id)).all()
    closed = [row for row in positions if row.status == "CLOSED"]
    realized = sum((row.realized_pnl for row in positions), Decimal(0)) if positions else Decimal(0)
    wins = [row for row in closed if row.realized_pnl > 0]
    executions = session.scalars(
        select(ExecutionRecord)
        .join(OrderRecord, OrderRecord.id == ExecutionRecord.order_id)
        .where(OrderRecord.account_id == account_id)
    ).all()
    cost = None
    if executions:
        cost = sum((row.commission + row.slippage_cost + row.spread_cost for row in executions), Decimal(0))
    return {
        "realized_pnl": realized,
        "unrealized_pnl": None,
        "trade_count": len(closed),
        "win_rate": None if not closed else Decimal(len(wins)) / Decimal(len(closed)),
        "cost_total": cost,
    }


def _drawdown(equities: list[Decimal]) -> Decimal | None:
    if len(equities) < 2:
        return None
    peak = equities[0]
    worst = Decimal(0)
    for equity in equities:
        peak = max(peak, equity)
        if peak > 0:
            worst = min(worst, (equity - peak) / peak)
    return worst


def _text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")
