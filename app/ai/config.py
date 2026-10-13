from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = PROJECT_ROOT / "config" / "ai.yaml"


@dataclass(frozen=True)
class AiConfig:
    feature_version: str = "v1"
    min_samples: int = 500
    min_symbols: int = 1
    min_calendar_days: int = 20
    min_regimes: int = 2
    min_folds: int = 2
    min_test_samples: int = 30
    embargo_minutes: int = 60
    adx_trend: float = 25
    adx_range: float = 20
    high_vol_atr_pct: float = 0.02
    high_vol_realized: float = 0.01
    day_horizons_bars: tuple[int, ...] = (3, 6, 12)
    swing_horizons_days: tuple[int, ...] = (1, 3, 5)
    seed: int = 7
    learner: str = "lightgbm"

    def with_overrides(self, overrides: dict | None) -> AiConfig:
        if not overrides:
            return self
        values = {}
        for key, value in overrides.items():
            if key not in self.__dataclass_fields__:
                continue
            current = getattr(self, key)
            if isinstance(current, tuple) and isinstance(value, list):
                values[key] = tuple(int(item) for item in value)
            elif isinstance(current, int) and not isinstance(current, bool):
                values[key] = int(value)
            elif isinstance(current, float):
                values[key] = float(value)
            else:
                values[key] = value
        return AiConfig(**{**self.__dict__, **values})


def load_ai_config(path: Path | None = None) -> AiConfig:
    raw = yaml.safe_load((path or DEFAULT_PATH).read_text(encoding="utf-8")) or {}
    horizons = raw.get("day_horizons_bars", [3, 6, 12])
    swing = raw.get("swing_horizons_days", [1, 3, 5])
    return AiConfig(
        feature_version=str(raw.get("feature_version", "v1")),
        min_samples=int(raw.get("min_samples", 500)),
        min_symbols=int(raw.get("min_symbols", 1)),
        min_calendar_days=int(raw.get("min_calendar_days", 20)),
        min_regimes=int(raw.get("min_regimes", 2)),
        min_folds=int(raw.get("min_folds", 2)),
        min_test_samples=int(raw.get("min_test_samples", 30)),
        embargo_minutes=int(raw.get("embargo_minutes", 60)),
        adx_trend=float(raw.get("adx_trend", 25)),
        adx_range=float(raw.get("adx_range", 20)),
        high_vol_atr_pct=float(raw.get("high_vol_atr_pct", 0.02)),
        high_vol_realized=float(raw.get("high_vol_realized", 0.01)),
        day_horizons_bars=tuple(int(item) for item in horizons),
        swing_horizons_days=tuple(int(item) for item in swing),
        seed=int(raw.get("seed", 7)),
        learner=str(raw.get("learner", "lightgbm")),
    )
