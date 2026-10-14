from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

BAR = timedelta(minutes=5)


@dataclass(frozen=True)
class FillQuote:
    price: Decimal
    reference: Decimal
    observed_at: datetime
    received_at: datetime
    source: str


def stop_is_valid(price: Decimal, stop: Decimal | None) -> bool:
    if price <= 0 or stop is None:
        return False
    return Decimal(0) < stop < price


def paper_fill_quote(
    *,
    side: str,
    submitted_at: datetime,
    bar_start: datetime,
    open_price: Decimal,
    close_price: Decimal,
    received_at: datetime,
    bid: Decimal | None = None,
    ask: Decimal | None = None,
    spread_pct: Decimal = Decimal(0),
    slippage_pct: Decimal = Decimal(0),
) -> FillQuote | None:
    """A paper fill may use only a price observed after the order was submitted.

    The next bar's open is the price at that bar's start. When the start is at or
    before submission, it is not a fill. Backtests do not use this function.
    """
    if received_at <= submitted_at:
        return None
    if side == "BUY" and ask is not None and ask > 0:
        return FillQuote(ask, ask, received_at, received_at, "ask")
    if side == "SELL" and bid is not None and bid > 0:
        return FillQuote(bid, bid, received_at, received_at, "bid")
    close_time = bar_start + BAR
    if close_time <= submitted_at:
        return None
    reference = close_price
    if side == "BUY":
        price = reference * (Decimal(1) + slippage_pct + spread_pct / Decimal(2))
    else:
        price = reference * (Decimal(1) - slippage_pct - spread_pct / Decimal(2))
    if price <= 0:
        return None
    return FillQuote(price, reference, close_time, received_at, "close")
