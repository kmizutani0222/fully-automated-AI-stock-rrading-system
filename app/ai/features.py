from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from app.ai.calendar import segment_id, session_bar_starts, zone_for
from app.data.normalizer import is_bar_closed
from app.trading.indicators import ema, rsi
from app.trading.strategies import PriceBar

BAR = timedelta(minutes=5)
DAY_FEATURES = (
    "return_1",
    "return_3",
    "return_6",
    "return_12",
    "ema_9",
    "ema_21",
    "ema_50",
    "sma_20",
    "sma_50",
    "rsi_14",
    "macd",
    "macd_signal",
    "macd_hist",
    "atr_14",
    "bb_mid",
    "bb_upper",
    "bb_lower",
    "bb_width",
    "vwap",
    "volume_change",
    "dist_high_20",
    "dist_low_20",
    "adx_14",
    "realized_vol_12",
    "tf15_return",
    "tf15_rsi_14",
    "tf60_return",
    "tf60_rsi_14",
    "tf1d_return",
    "tf1d_rsi_14",
)


@dataclass(frozen=True)
class FeatureRow:
    symbol: str
    timestamp: datetime
    available_at: datetime
    values: dict[str, float | None]


@dataclass(frozen=True)
class DailyBar:
    market: str
    symbol: str
    day: object
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    available_at: datetime


def available_at(timestamp: datetime) -> datetime:
    return timestamp + BAR


def closed_bars(bars: list[PriceBar], now: datetime | None = None) -> list[PriceBar]:
    return [bar for bar in bars if is_bar_closed(bar.timestamp, now)]


def compute_features(bars: list[PriceBar], market: str, *, now: datetime | None = None) -> list[FeatureRow]:
    """Features at bar t use only bars that are already closed at t. Higher timeframes use completed buckets."""
    usable = closed_bars(sorted(bars, key=lambda bar: bar.timestamp), now)
    if not usable:
        return []
    closes = [_float(bar.close) for bar in usable]
    highs = [_float(bar.high) for bar in usable]
    lows = [_float(bar.low) for bar in usable]
    volumes = [None if bar.volume is None else _float(bar.volume) for bar in usable]
    decimal_closes = [Decimal(str(value)) for value in closes]
    ema_9 = _as_float(ema(decimal_closes, 9))
    ema_21 = _as_float(ema(decimal_closes, 21))
    ema_50 = _as_float(ema(decimal_closes, 50))
    rsi_14 = _as_float(rsi(decimal_closes, 14))
    sma_20 = _sma(closes, 20)
    sma_50 = _sma(closes, 50)
    macd_line, macd_signal, macd_hist = _macd(closes)
    atr = _atr(highs, lows, closes, 14)
    adx = _adx(highs, lows, closes, 14)
    bb_mid, bb_upper, bb_lower, bb_width = _bollinger(closes, 20)
    realized = _realized_vol(closes, 12)
    vwap = _session_vwap(usable, market, highs, lows, closes, volumes)
    tf15 = _higher_frame(usable, market, 15)
    tf60 = _higher_frame(usable, market, 60)
    daily = _completed_days(usable, market)
    daily_rsi = _as_float(rsi([Decimal(str(bar.close)) for bar in daily], 14)) if daily else []
    rows: list[FeatureRow] = []
    for index, bar in enumerate(usable):
        close = closes[index]
        values: dict[str, float | None] = {
            "return_1": _return(closes, index, 1),
            "return_3": _return(closes, index, 3),
            "return_6": _return(closes, index, 6),
            "return_12": _return(closes, index, 12),
            "ema_9": ema_9[index],
            "ema_21": ema_21[index],
            "ema_50": ema_50[index],
            "sma_20": sma_20[index],
            "sma_50": sma_50[index],
            "rsi_14": rsi_14[index],
            "macd": macd_line[index],
            "macd_signal": macd_signal[index],
            "macd_hist": macd_hist[index],
            "atr_14": atr[index],
            "bb_mid": bb_mid[index],
            "bb_upper": bb_upper[index],
            "bb_lower": bb_lower[index],
            "bb_width": bb_width[index],
            "vwap": vwap[index],
            "volume_change": _volume_change(volumes, index),
            "dist_high_20": _distance(close, _window_extreme(highs, index, 20, max)),
            "dist_low_20": _distance(close, _window_extreme(lows, index, 20, min)),
            "adx_14": adx[index],
            "realized_vol_12": realized[index],
            "tf15_return": tf15["return"][index],
            "tf15_rsi_14": tf15["rsi"][index],
            "tf60_return": tf60["return"][index],
            "tf60_rsi_14": tf60["rsi"][index],
            "tf1d_return": _daily_value(daily, daily_return(daily), bar, index_ready=1),
            "tf1d_rsi_14": _daily_value(daily, daily_rsi, bar, index_ready=14),
        }
        rows.append(FeatureRow(bar.symbol, bar.timestamp, available_at(bar.timestamp), values))
    return rows


def _macd(closes: list[float]) -> tuple[list[float | None], list[float | None], list[float | None]]:
    fast = _as_float(ema([Decimal(str(value)) for value in closes], 12))
    slow = _as_float(ema([Decimal(str(value)) for value in closes], 26))
    line: list[float | None] = [None if fast[i] is None or slow[i] is None else fast[i] - slow[i] for i in range(len(closes))]
    signal = _ema_optional(line, 9)
    hist = [None if line[i] is None or signal[i] is None else line[i] - signal[i] for i in range(len(closes))]
    return line, signal, hist


def _ema_optional(values: list[float | None], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    numeric = [value for value in values if value is not None]
    if len(numeric) < period:
        return result
    converted = _as_float(ema([Decimal(str(value)) for value in numeric], period))
    cursor = 0
    for index, value in enumerate(values):
        if value is None:
            continue
        result[index] = converted[cursor]
        cursor += 1
    return result


def _sma(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return result
    window = sum(values[:period])
    result[period - 1] = window / period
    for index in range(period, len(values)):
        window += values[index] - values[index - period]
        result[index] = window / period
    return result


def _return(values: list[float], index: int, periods: int) -> float | None:
    if index < periods or values[index - periods] == 0:
        return None
    return values[index] / values[index - periods] - 1


def _volume_change(volumes: list[float | None], index: int) -> float | None:
    if index == 0:
        return None
    current = volumes[index]
    previous = volumes[index - 1]
    if current is None or previous is None or previous == 0:
        return None
    return current / previous - 1


def _window_extreme(values: list[float], index: int, period: int, reducer) -> float | None:
    if index + 1 < period:
        return None
    return reducer(values[index - period + 1 : index + 1])


def _distance(close: float, extreme: float | None) -> float | None:
    if extreme is None or close == 0:
        return None
    return (close - extreme) / close


def _bollinger(values: list[float], period: int) -> tuple[list[float | None], list[float | None], list[float | None], list[float | None]]:
    mid = _sma(values, period)
    upper: list[float | None] = [None] * len(values)
    lower: list[float | None] = [None] * len(values)
    width: list[float | None] = [None] * len(values)
    for index, center in enumerate(mid):
        if center is None:
            continue
        window = values[index - period + 1 : index + 1]
        deviation = _population_std(window)
        if deviation is None:
            continue
        upper[index] = center + 2 * deviation
        lower[index] = center - 2 * deviation
        width[index] = None if center == 0 else (upper[index] - lower[index]) / center
    return mid, upper, lower, width


def _population_std(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return math.sqrt(variance)


def _realized_vol(closes: list[float], period: int) -> list[float | None]:
    returns = [None if index == 0 or closes[index - 1] == 0 else closes[index] / closes[index - 1] - 1 for index in range(len(closes))]
    result: list[float | None] = [None] * len(closes)
    for index in range(len(closes)):
        window = [value for value in returns[max(0, index - period + 1) : index + 1] if value is not None]
        if len(window) < period:
            continue
        result[index] = _population_std(window)
    return result


def _true_ranges(highs: list[float], lows: list[float], closes: list[float]) -> list[float | None]:
    ranges: list[float | None] = [None]
    for index in range(1, len(closes)):
        ranges.append(max(highs[index] - lows[index], abs(highs[index] - closes[index - 1]), abs(lows[index] - closes[index - 1])))
    return ranges


def _wilder(values: list[float | None], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    numeric_at = [index for index, value in enumerate(values) if value is not None]
    if len(numeric_at) < period:
        return result
    seed_indexes = numeric_at[:period]
    seed = sum(values[index] for index in seed_indexes) / period
    result[seed_indexes[-1]] = seed
    previous = seed
    for index in numeric_at[period:]:
        previous = (previous * (period - 1) + values[index]) / period
        result[index] = previous
    return result


def _atr(highs: list[float], lows: list[float], closes: list[float], period: int) -> list[float | None]:
    return _wilder(_true_ranges(highs, lows, closes), period)


def _adx(highs: list[float], lows: list[float], closes: list[float], period: int) -> list[float | None]:
    size = len(closes)
    plus_dm: list[float | None] = [None]
    minus_dm: list[float | None] = [None]
    for index in range(1, size):
        up = highs[index] - highs[index - 1]
        down = lows[index - 1] - lows[index]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
    tr = _wilder(_true_ranges(highs, lows, closes), period)
    smooth_plus = _wilder(plus_dm, period)
    smooth_minus = _wilder(minus_dm, period)
    dx: list[float | None] = [None] * size
    for index in range(size):
        if None in {tr[index], smooth_plus[index], smooth_minus[index]} or tr[index] == 0:
            continue
        plus_di = 100 * smooth_plus[index] / tr[index]
        minus_di = 100 * smooth_minus[index] / tr[index]
        total = plus_di + minus_di
        if total == 0:
            continue
        dx[index] = 100 * abs(plus_di - minus_di) / total
    return _wilder(dx, period)


def _session_vwap(bars: list[PriceBar], market: str, highs, lows, closes, volumes) -> list[float | None]:
    result: list[float | None] = [None] * len(bars)
    bucket = None
    price_volume = 0.0
    total_volume = 0.0
    for index, bar in enumerate(bars):
        current = segment_id(market, bar.timestamp)
        if current != bucket:
            bucket = current
            price_volume = 0.0
            total_volume = 0.0
        volume = volumes[index]
        if current is None or volume is None or volume <= 0:
            continue
        typical = (highs[index] + lows[index] + closes[index]) / 3
        price_volume += typical * volume
        total_volume += volume
        result[index] = price_volume / total_volume
    return result


def _higher_frame(bars: list[PriceBar], market: str, minutes: int) -> dict[str, list[float | None]]:
    """A higher-timeframe value appears only after its last 5-minute bar has closed."""
    step = minutes // 5
    returns: list[float | None] = [None] * len(bars)
    rsis: list[float | None] = [None] * len(bars)
    by_time = {bar.timestamp: index for index, bar in enumerate(bars)}
    completed: list[tuple[int, float]] = []
    for bar in bars:
        start = bar.timestamp
        if start.minute % minutes != 0:
            continue
        slots = [start + BAR * offset for offset in range(step)]
        if any(slot not in by_time for slot in slots):
            continue
        if any(segment_id(market, slot) != segment_id(market, start) for slot in slots):
            continue
        last_index = by_time[slots[-1]]
        completed.append((last_index, float(bars[last_index].close)))
    cursor = 0
    running_return: float | None = None
    running_rsi: float | None = None
    frame_closes: list[float] = []
    for index in range(len(bars)):
        while cursor < len(completed) and completed[cursor][0] <= index:
            frame_closes.append(completed[cursor][1])
            previous = frame_closes[-2] if len(frame_closes) >= 2 else None
            close = frame_closes[-1]
            running_return = None if previous in {None, 0} else close / previous - 1
            running_rsi = _as_float(rsi([Decimal(str(value)) for value in frame_closes], 14))[-1]
            cursor += 1
        if cursor == 0:
            continue
        returns[index] = running_return
        rsis[index] = running_rsi
    return {"return": returns, "rsi": rsis}


def _completed_days(bars: list[PriceBar], market: str) -> list[DailyBar]:
    grouped: dict[object, list[PriceBar]] = {}
    for bar in bars:
        local_day = bar.timestamp.astimezone(zone_for(market)).date()
        grouped.setdefault(local_day, []).append(bar)
    days: list[DailyBar] = []
    for day, rows in grouped.items():
        expected = session_bar_starts(market, day)
        if not expected:
            continue
        have = {row.timestamp for row in rows}
        if any(start not in have for start in expected):
            continue
        ordered = sorted(rows, key=lambda row: row.timestamp)
        volume_values = [row.volume for row in ordered]
        volume = None if any(value is None for value in volume_values) else float(sum(volume_values, Decimal(0)))
        days.append(
            DailyBar(
                market=market,
                symbol=ordered[-1].symbol,
                day=day,
                open=float(ordered[0].open),
                high=max(float(row.high) for row in ordered),
                low=min(float(row.low) for row in ordered),
                close=float(ordered[-1].close),
                volume=volume,
                available_at=available_at(ordered[-1].timestamp),
            )
        )
    return days


def daily_return(days: list[DailyBar]) -> list[float | None]:
    result: list[float | None] = [None]
    for index in range(1, len(days)):
        previous = days[index - 1].close
        result.append(None if previous == 0 else days[index].close / previous - 1)
    return result


def _daily_value(days: list[DailyBar], values: list[float | None], bar: PriceBar, *, index_ready: int) -> float | None:
    chosen = None
    ready = available_at(bar.timestamp)
    for index, day in enumerate(days):
        if day.available_at <= ready and index >= index_ready and index < len(values):
            chosen = values[index]
    return chosen


def _as_float(values: list[Decimal | None]) -> list[float | None]:
    return [None if value is None else float(value) for value in values]


def _float(value: Decimal) -> float:
    return float(value)


def completed_daily_bars(bars: list[PriceBar], market: str, *, now: datetime | None = None) -> list[DailyBar]:
    usable = closed_bars(sorted(bars, key=lambda bar: bar.timestamp), now)
    return _completed_days(usable, market)
