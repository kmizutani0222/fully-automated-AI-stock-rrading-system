from __future__ import annotations

import math
from decimal import Decimal

from app.broker.simulated import Execution
from app.portfolio.ledger import Portfolio


def compute_metrics(executions: list[Execution], equities: list[Decimal], initial_equity: Decimal) -> dict:
    sells = [item for item in executions if item.side == "SELL" and item.realized_pnl is not None]
    closed = [item.realized_pnl for item in sells if item.realized_pnl is not None]
    wins = [value for value in closed if value > 0]
    losses = [value for value in closed if value < 0]
    realized = sum(closed, Decimal(0))
    last_equity = equities[-1] if equities else None
    unrealized = None if last_equity is None else last_equity - initial_equity - realized
    total_pnl = None if unrealized is None else realized + unrealized
    return {
        "total_pnl": _text(total_pnl),
        "realized_pnl": _text(realized if executions or equities else None),
        "unrealized_pnl": _text(unrealized),
        "total_return": _text(None if total_pnl is None or initial_equity == 0 else total_pnl / initial_equity),
        "win_rate": _text(None if not closed else Decimal(len(wins)) / Decimal(len(closed))),
        "profit_factor": _profit_factor(wins, losses),
        "max_drawdown": _drawdown(equities),
        "sharpe_ratio": _sharpe(equities),
        "average_profit": _text(None if not wins else sum(wins, Decimal(0)) / Decimal(len(wins))),
        "average_loss": _text(None if not losses else sum(losses, Decimal(0)) / Decimal(len(losses))),
        "trade_count": len(executions),
        "closed_trades": len(closed),
        "commission_total": _text(sum((item.commission for item in executions), Decimal(0))),
        "slippage_total": _text(sum((item.slippage_cost for item in executions), Decimal(0))),
    }


def ending_unrealized(portfolio: Portfolio, currency: str, initial_equity: Decimal, realized: Decimal) -> Decimal | None:
    equity = portfolio.equity(currency)
    if equity is None:
        return None
    return equity - initial_equity - realized


def _profit_factor(wins: list[Decimal], losses: list[Decimal]) -> str | None:
    if not wins and not losses:
        return None
    if not losses:
        return None
    gross_loss = abs(sum(losses, Decimal(0)))
    if gross_loss == 0:
        return None
    return _text(sum(wins, Decimal(0)) / gross_loss)


def _drawdown(equities: list[Decimal]) -> str | None:
    if not equities:
        return None
    peak = equities[0]
    worst = Decimal(0)
    for equity in equities:
        if equity > peak:
            peak = equity
        if peak > 0:
            worst = max(worst, (peak - equity) / peak)
    return _text(worst)


def _sharpe(equities: list[Decimal]) -> str | None:
    if len(equities) < 3:
        return None
    returns: list[float] = []
    for previous, current in zip(equities, equities[1:], strict=False):
        if previous == 0:
            return None
        returns.append(float((current - previous) / previous))
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    if variance <= 0:
        return None
    per_bar = mean / math.sqrt(variance)
    annualized = per_bar * math.sqrt(252 * 78)
    return str(Decimal(str(annualized)).quantize(Decimal("0.0001")))


def _text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")
