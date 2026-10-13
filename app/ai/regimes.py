from __future__ import annotations

from abc import ABC, abstractmethod

from app.ai.config import AiConfig

REGIMES = ("TREND_UP", "TREND_DOWN", "RANGE", "HIGH_VOL", "UNCERTAIN")


class RegimeClassifier(ABC):
    method: str

    @abstractmethod
    def classify(self, values: dict[str, float | None]) -> str:
        """Classify one feature row. A later LightGBM classifier can replace the rule model."""


class RuleRegimeClassifier(RegimeClassifier):
    method = "rule"

    def __init__(self, config: AiConfig) -> None:
        self.config = config

    def classify(self, values: dict[str, float | None]) -> str:
        required = ("ema_9", "ema_21", "ema_50", "adx_14", "atr_14", "return_12", "realized_vol_12")
        if any(values.get(name) is None for name in required):
            return "UNCERTAIN"
        close_scale = abs(values["ema_21"] or 0)
        atr_pct = None if close_scale == 0 else values["atr_14"] / close_scale
        if atr_pct is not None and (atr_pct >= self.config.high_vol_atr_pct or values["realized_vol_12"] >= self.config.high_vol_realized):
            return "HIGH_VOL"
        rising = values["ema_9"] > values["ema_21"] > values["ema_50"] and values["return_12"] > 0
        falling = values["ema_9"] < values["ema_21"] < values["ema_50"] and values["return_12"] < 0
        if values["adx_14"] >= self.config.adx_trend and rising:
            return "TREND_UP"
        if values["adx_14"] >= self.config.adx_trend and falling:
            return "TREND_DOWN"
        if values["adx_14"] < self.config.adx_range:
            return "RANGE"
        return "UNCERTAIN"


class LightGBMRegimeClassifier(RegimeClassifier):
    """Reserved for a later replacement. Phase 3 does not train this classifier."""

    method = "lightgbm"

    def classify(self, values: dict[str, float | None]) -> str:
        raise NotImplementedError("The LightGBM regime classifier is not trained in this phase")


def build_regime_classifier(name: str, config: AiConfig) -> RegimeClassifier:
    if name == "rule":
        return RuleRegimeClassifier(config)
    if name == "lightgbm":
        return LightGBMRegimeClassifier()
    raise KeyError(name)
