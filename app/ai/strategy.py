from __future__ import annotations

from app.ai.selector import Decision
from app.trading.strategies import PriceBar, Signal, Strategy


class ReplayStrategy(Strategy):
    """Replay decisions that were already scored. Predictions never reach the broker directly."""

    name = "ai_replay"
    version = "v1"

    def __init__(self, decisions: dict[tuple[str, object], Decision]) -> None:
        self.decisions = decisions

    @property
    def minimum_bars(self) -> int:
        return 1

    def evaluate(self, bars: list[PriceBar]) -> Signal | None:
        if not bars:
            return None
        decision = self.decisions.get((bars[-1].symbol, bars[-1].timestamp))
        if decision is None or decision.choice not in {"DAY", "SWING"}:
            return None
        return Signal(bars[-1].symbol, bars[-1].timestamp, "BUY", decision.reason)
