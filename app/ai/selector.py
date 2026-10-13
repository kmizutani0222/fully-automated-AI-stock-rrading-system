from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Forecast:
    symbol: str
    style: str
    horizon_steps: int
    expected_return: float | None
    up_probability: float | None
    risk: float | None
    gap_risk: float | None
    available_at: datetime
    expires_at: datetime
    is_out_of_sample: bool


@dataclass(frozen=True)
class Decision:
    symbol: str | None
    choice: str
    reason: str
    score: float | None
    managed_styles: dict[str, str] = field(default_factory=dict)


def score_forecast(forecast: Forecast, cost: float, *, now: datetime) -> float | None:
    problem = forecast_problem(forecast, now)
    if problem:
        return None
    risk = forecast.risk if forecast.style == "DAY" else _swing_risk(forecast)
    if risk is None or risk <= 0 or forecast.horizon_steps <= 0 or forecast.expected_return is None:
        return None
    net = forecast.expected_return - cost
    return net / (risk * math.sqrt(forecast.horizon_steps))


def forecast_problem(forecast: Forecast, now: datetime) -> str | None:
    if not forecast.is_out_of_sample:
        return "prediction_insample"
    if forecast.expires_at <= now:
        return "prediction_expired"
    if forecast.expected_return is None or forecast.up_probability is None or forecast.risk is None:
        return "prediction_invalid"
    if forecast.up_probability < 0 or forecast.up_probability > 1:
        return "prediction_invalid"
    return None


def select_strategy(
    symbol: str,
    day: list[Forecast],
    swing: list[Forecast],
    *,
    cost: float,
    now: datetime,
    open_style: str | None = None,
) -> Decision:
    """Choose DAY, SWING, or CASH. This does not place an order."""
    managed = {} if open_style not in {"DAY", "SWING"} else {symbol: open_style}
    if open_style in {"DAY", "SWING"}:
        return Decision(symbol, "CASH", "open position keeps its original style", None, managed)
    problems = [forecast_problem(item, now) for item in (*day, *swing)]
    present = [item for item in problems if item]
    if not day and not swing:
        return Decision(symbol, "CASH", "prediction_missing", None, managed)
    if present and all(item is not None for item in problems):
        return Decision(symbol, "CASH", present[0], None, managed)
    day_score = _best(day, cost, now)
    swing_score = _best(swing, cost, now)
    if day_score is None and swing_score is None:
        reason = present[0] if present else "prediction_invalid"
        return Decision(symbol, "CASH", reason, None, managed)
    if swing_score is None or (day_score is not None and day_score >= swing_score):
        if day_score is None or day_score <= 0:
            return Decision(symbol, "CASH", "cost_exceeds_expected_return", day_score, managed)
        return Decision(symbol, "DAY", "day_score_above_swing_and_cost", day_score, managed)
    if swing_score <= 0:
        return Decision(symbol, "CASH", "cost_exceeds_expected_return", swing_score, managed)
    return Decision(symbol, "SWING", "swing_score_above_day_and_cost", swing_score, managed)


def _best(forecasts: list[Forecast], cost: float, now: datetime) -> float | None:
    scores = [score_forecast(item, cost, now=now) for item in forecasts]
    usable = [score for score in scores if score is not None]
    if not usable:
        return None
    return max(usable)


def _swing_risk(forecast: Forecast) -> float | None:
    if forecast.risk is None or forecast.gap_risk is None:
        return None
    return forecast.risk + forecast.gap_risk
