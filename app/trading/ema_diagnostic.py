from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.data.normalizer import is_bar_closed
from app.data.repository import get_instrument, load_bars
from app.trading.config import load_trading_config
from app.trading.indicators import ema


def diagnose_ema_series(
    closes: list[Decimal],
    timestamps: list[datetime],
    *,
    fast_period: int = 9,
    slow_period: int = 21,
) -> list[dict]:
    """One read-only diagnosis per bar. Earlier bars stay in the EMA, and nothing is stored."""
    fast_values = ema(closes, fast_period)
    slow_values = ema(closes, slow_period)
    minimum = slow_period + 1
    rows = []
    for index, (close, timestamp) in enumerate(zip(closes, timestamps, strict=True)):
        previous = index - 1
        current_fast = fast_values[index]
        current_slow = slow_values[index]
        previous_fast = fast_values[previous] if previous >= 0 else None
        previous_slow = slow_values[previous] if previous >= 0 else None
        sufficient = None not in {current_fast, current_slow, previous_fast, previous_slow}
        cross = None
        if sufficient:
            cross = _cross(previous_fast, previous_slow, current_fast, current_slow)
        reason, reason_text = _reason(
            sufficient=sufficient,
            cross=cross,
            previous_fast=previous_fast,
            previous_slow=previous_slow,
            current_fast=current_fast,
            current_slow=current_slow,
            bar_count=index + 1,
            minimum=minimum,
            fast_period=fast_period,
            slow_period=slow_period,
        )
        rows.append(
            {
                "timestamp": timestamp,
                "close": _text(close),
                "bar_count": index + 1,
                "minimum_bars": minimum,
                "sufficient": sufficient,
                "ema_fast": _text(current_fast),
                "ema_slow": _text(current_slow),
                "previous_ema_fast": _text(previous_fast),
                "previous_ema_slow": _text(previous_slow),
                "ema_fast_change": _text(_subtract(current_fast, previous_fast)),
                "ema_slow_change": _text(_subtract(current_slow, previous_slow)),
                "spread": _text(_subtract(current_fast, current_slow)),
                "previous_spread": _text(_subtract(previous_fast, previous_slow)),
                "cross": "none" if sufficient and cross is None else cross,
                "signal": {"up": "BUY", "down": "SELL"}.get(cross or "", None),
                "reason": reason,
                "reason_text": reason_text,
            }
        )
    return rows


def ema_diagnostic(session: Session, *, symbol: str = "AAPL", market: str = "US", limit: int = 100, now: datetime | None = None) -> dict:
    config = load_trading_config()
    fast_period = config.ema_fast
    slow_period = config.ema_slow
    current = now or datetime.now(timezone.utc)
    instrument = get_instrument(session, market, symbol)
    if instrument is None:
        return _empty(symbol, market, fast_period, slow_period, "確定した5分足がありません。")
    selected: dict[datetime, object] = {}
    for row in sorted(load_bars(session, instrument.id, "5m"), key=lambda item: (item.timestamp, item.id)):
        selected[row.timestamp] = row
    closed = [selected[key] for key in sorted(selected) if is_bar_closed(key, current)]
    if not closed:
        return _empty(symbol, market, fast_period, slow_period, "確定した5分足がありません。")
    diagnoses = diagnose_ema_series(
        [row.close for row in closed],
        [row.timestamp for row in closed],
        fast_period=fast_period,
        slow_period=slow_period,
    )
    window = max(1, min(limit, 5000))
    history = list(reversed(diagnoses[-window:]))
    latest = history[0] if history else None
    return {
        "symbol": symbol.upper(),
        "market": market.upper(),
        "timeframe": "5m",
        "fast_period": fast_period,
        "slow_period": slow_period,
        "minimum_bars": slow_period + 1,
        "bar_count": len(diagnoses),
        "read_only": True,
        "latest": latest,
        "history": history,
    }


def _empty(symbol: str, market: str, fast_period: int, slow_period: int, reason_text: str) -> dict:
    return {
        "symbol": symbol.upper(),
        "market": market.upper(),
        "timeframe": "5m",
        "fast_period": fast_period,
        "slow_period": slow_period,
        "minimum_bars": slow_period + 1,
        "bar_count": 0,
        "read_only": True,
        "latest": {
            "timestamp": None,
            "close": None,
            "bar_count": 0,
            "minimum_bars": slow_period + 1,
            "sufficient": False,
            "ema_fast": None,
            "ema_slow": None,
            "previous_ema_fast": None,
            "previous_ema_slow": None,
            "ema_fast_change": None,
            "ema_slow_change": None,
            "spread": None,
            "previous_spread": None,
            "cross": None,
            "signal": None,
            "reason": "insufficient_data",
            "reason_text": reason_text,
        },
        "history": [],
    }


def _cross(previous_fast: Decimal, previous_slow: Decimal, current_fast: Decimal, current_slow: Decimal) -> str | None:
    if previous_fast <= previous_slow and current_fast > current_slow:
        return "up"
    if previous_fast >= previous_slow and current_fast < current_slow:
        return "down"
    return None


def _reason(
    *,
    sufficient: bool,
    cross: str | None,
    previous_fast: Decimal | None,
    previous_slow: Decimal | None,
    current_fast: Decimal | None,
    current_slow: Decimal | None,
    bar_count: int,
    minimum: int,
    fast_period: int,
    slow_period: int,
) -> tuple[str, str]:
    if not sufficient:
        return (
            "insufficient_data",
            f"データ不足。EMA{fast_period}とEMA{slow_period}の交差判定には確定足が{minimum}本必要です。現在{bar_count}本です。",
        )
    if cross == "up":
        return "cross_up", f"EMA{fast_period}がEMA{slow_period}を上抜けました。"
    if cross == "down":
        return "cross_down", f"EMA{fast_period}がEMA{slow_period}を下抜けました。"
    assert previous_fast is not None and previous_slow is not None and current_fast is not None and current_slow is not None
    if previous_fast > previous_slow and current_fast > current_slow:
        return "no_cross", f"クロスなし。EMA{fast_period}はEMA{slow_period}より上のままです。"
    if previous_fast < previous_slow and current_fast < current_slow:
        return "no_cross", f"クロスなし。EMA{fast_period}はEMA{slow_period}より下のままです。"
    return "no_cross", f"クロスなし。EMA{fast_period}とEMA{slow_period}の上下は入れ替わっていません。"


def _subtract(left: Decimal | None, right: Decimal | None) -> Decimal | None:
    if left is None or right is None:
        return None
    return left - right


def _text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")
