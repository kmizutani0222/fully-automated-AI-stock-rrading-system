from __future__ import annotations

import math
from datetime import timedelta
from decimal import Decimal

from app.ai.calendar import next_trading_days, segment_id, zone_for
from app.ai.features import DailyBar, FeatureRow, daily_return
from app.trading.indicators import rsi
from app.trading.strategies import PriceBar

BAR = timedelta(minutes=5)
DAY_COLUMNS = (
    "return_1",
    "return_3",
    "return_6",
    "return_12",
    "ema_9",
    "ema_21",
    "sma_20",
    "rsi_14",
    "atr_14",
    "realized_vol_12",
    "dist_high_20",
    "dist_low_20",
)
SWING_COLUMNS = (
    "daily_return",
    "daily_sma_20",
    "daily_rsi_14",
    "daily_atr_14",
    "dist_high_20",
    "dist_low_20",
    "volume_trend_20",
    "relative_strength",
    "gap_risk",
)


def day_label(bars: list[PriceBar], index: int, horizon: int, market: str) -> float | None:
    """Forward return of `horizon` closed 5-minute bars. A path that leaves the session is unlabeled."""
    target = index + horizon
    if target >= len(bars) or bars[index].close == 0:
        return None
    start_segment = segment_id(market, bars[index].timestamp)
    if start_segment is None:
        return None
    for step in range(1, horizon + 1):
        current = bars[index + step]
        previous = bars[index + step - 1]
        if current.timestamp - previous.timestamp != BAR:
            return None
        if segment_id(market, current.timestamp) != start_segment:
            return None
    return float(bars[target].close / bars[index].close - 1)


def swing_samples(days: list[DailyBar], index_days: list[DailyBar] | None, market: str, horizons: tuple[int, ...]) -> list[dict]:
    """One row per completed session. Relative strength and gap risk use only sessions already closed."""
    if not days:
        return []
    returns = daily_return(days)
    index_returns = daily_return(index_days) if index_days else []
    index_return_by_day = {day.day: index_returns[index] for index, day in enumerate(index_days)} if index_days else {}
    closes = [day.close for day in days]
    sma_20 = _sma(closes, 20)
    rsi_14 = [None if value is None else float(value) for value in rsi([Decimal(str(value)) for value in closes], 14)]
    atr = _daily_atr(days, 14)
    samples = []
    for index, day in enumerate(days):
        labels = {}
        label_ends = {}
        for horizon in horizons:
            future = next_trading_days(market, day.day, horizon)
            if len(future) < horizon:
                continue
            target = next((item for item in days if item.day == future[-1]), None)
            if target is None or day.close == 0:
                continue
            name = f"return_{horizon}d"
            labels[name] = target.close / day.close - 1
            label_ends[name] = target.available_at
        if not labels:
            continue
        gap = _gap_risk(days, index)
        stock_return = returns[index]
        index_return = index_return_by_day.get(day.day)
        relative = None if stock_return is None or index_return is None else stock_return - index_return
        sample_features = {
            "daily_return": stock_return,
            "daily_sma_20": sma_20[index],
            "daily_rsi_14": rsi_14[index],
            "daily_atr_14": atr[index],
            "dist_high_20": _distance(day.close, _extreme([item.high for item in days], index, 20, max)),
            "dist_low_20": _distance(day.close, _extreme([item.low for item in days], index, 20, min)),
            "volume_trend_20": _volume_trend(days, index),
            "relative_strength": relative,
            "gap_risk": gap,
        }
        if any(sample_features.get(name) is None for name in SWING_COLUMNS):
            continue
        samples.append(
            {
                "symbol": day.symbol,
                "day": day.day,
                "timestamp": day.available_at - BAR,
                "available_at": day.available_at,
                "label_end": max(label_ends.values()),
                "label_ends": label_ends,
                "features": {name: sample_features[name] for name in SWING_COLUMNS},
                "labels": labels,
                "gap_risk": gap,
            }
        )
    return samples


def day_samples(rows: list[FeatureRow], bars: list[PriceBar], market: str, horizons: tuple[int, ...]) -> list[dict]:
    by_time = {bar.timestamp: index for index, bar in enumerate(bars)}
    samples = []
    for row in rows:
        index = by_time.get(row.timestamp)
        if index is None:
            continue
        labels = {}
        label_ends = {}
        for horizon in horizons:
            value = day_label(bars, index, horizon, market)
            if value is None:
                continue
            name = f"return_{horizon * 5}m"
            labels[name] = value
            label_ends[name] = bars[index + horizon].timestamp + BAR
        if not labels:
            continue
        if any(row.values.get(name) is None for name in DAY_COLUMNS):
            continue
        samples.append(
            {
                "symbol": row.symbol,
                "timestamp": row.timestamp,
                "available_at": row.available_at,
                "label_end": max(label_ends.values()),
                "label_ends": label_ends,
                "features": {name: row.values[name] for name in DAY_COLUMNS},
                "labels": labels,
                "regime_ready": row.values,
            }
        )
    return samples


def _gap_risk(days: list[DailyBar], index: int, lookback: int = 20) -> float | None:
    gaps = []
    start = max(1, index - lookback + 1)
    for cursor in range(start, index + 1):
        previous_close = days[cursor - 1].close
        if previous_close == 0:
            continue
        gaps.append(days[cursor].open / previous_close - 1)
    if len(gaps) < 5:
        return None
    mean = sum(gaps) / len(gaps)
    variance = sum((value - mean) ** 2 for value in gaps) / len(gaps)
    return math.sqrt(variance)


def _sma(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    window = sum(values[:period])
    result[period - 1] = window / period
    for index in range(period, len(values)):
        window += values[index] - values[index - period]
        result[index] = window / period
    return result


def _daily_atr(days: list[DailyBar], period: int) -> list[float | None]:
    ranges: list[float | None] = [None]
    for index in range(1, len(days)):
        previous = days[index - 1].close
        ranges.append(max(days[index].high - days[index].low, abs(days[index].high - previous), abs(days[index].low - previous)))
    result: list[float | None] = [None] * len(days)
    numeric = [(index, value) for index, value in enumerate(ranges) if value is not None]
    if len(numeric) < period:
        return result
    seed = sum(value for _, value in numeric[:period]) / period
    result[numeric[period - 1][0]] = seed
    previous = seed
    for index, value in numeric[period:]:
        previous = (previous * (period - 1) + value) / period
        result[index] = previous
    return result


def _extreme(values: list[float], index: int, period: int, reducer) -> float | None:
    if index + 1 < period:
        return None
    return reducer(values[index - period + 1 : index + 1])


def _distance(close: float, extreme: float | None) -> float | None:
    if extreme is None or close == 0:
        return None
    return (close - extreme) / close


def _volume_trend(days: list[DailyBar], index: int) -> float | None:
    window = [day.volume for day in days[max(0, index - 19) : index + 1]]
    if len(window) < 20 or any(value is None for value in window) or window[0] == 0:
        return None
    return window[-1] / window[0] - 1


def session_dates(bars: list[PriceBar], market: str) -> set:
    return {bar.timestamp.astimezone(zone_for(market)).date() for bar in bars if segment_id(market, bar.timestamp)}
