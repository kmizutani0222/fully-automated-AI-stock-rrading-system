from __future__ import annotations

from decimal import Decimal


def ema(closes: list[Decimal], period: int) -> list[Decimal | None]:
    """Causal EMA. The seed is the SMA of the first `period` closes."""
    values: list[Decimal | None] = [None] * len(closes)
    if period <= 0 or len(closes) < period:
        return values
    weight = Decimal(2) / Decimal(period + 1)
    seed = sum(closes[:period], Decimal(0)) / Decimal(period)
    values[period - 1] = seed
    previous = seed
    for index in range(period, len(closes)):
        previous = closes[index] * weight + previous * (Decimal(1) - weight)
        values[index] = previous
    return values


def rsi(closes: list[Decimal], period: int) -> list[Decimal | None]:
    """Causal Wilder RSI. Index t uses only closes up to t."""
    values: list[Decimal | None] = [None] * len(closes)
    if period <= 0 or len(closes) <= period:
        return values
    gains: list[Decimal] = []
    losses: list[Decimal] = []
    for index in range(1, len(closes)):
        change = closes[index] - closes[index - 1]
        gains.append(change if change > 0 else Decimal(0))
        losses.append(-change if change < 0 else Decimal(0))
    average_gain = sum(gains[:period], Decimal(0)) / Decimal(period)
    average_loss = sum(losses[:period], Decimal(0)) / Decimal(period)
    values[period] = _rsi_value(average_gain, average_loss)
    for index in range(period, len(gains)):
        average_gain = (average_gain * Decimal(period - 1) + gains[index]) / Decimal(period)
        average_loss = (average_loss * Decimal(period - 1) + losses[index]) / Decimal(period)
        values[index + 1] = _rsi_value(average_gain, average_loss)
    return values


def _rsi_value(average_gain: Decimal, average_loss: Decimal) -> Decimal:
    if average_gain == 0 and average_loss == 0:
        return Decimal(50)
    if average_loss == 0:
        return Decimal(100)
    relative = average_gain / average_loss
    return Decimal(100) - (Decimal(100) / (Decimal(1) + relative))
