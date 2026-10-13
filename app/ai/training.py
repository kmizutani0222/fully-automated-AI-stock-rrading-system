from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import timedelta

from app.ai.config import AiConfig
from app.ai.labels import DAY_COLUMNS, SWING_COLUMNS
from app.ai.regimes import RuleRegimeClassifier
from app.ai.splits import Fold, walk_forward


@dataclass
class Prediction:
    symbol: str
    timestamp: object
    available_at: object
    expires_at: object
    horizon: str
    expected_return: float | None
    up_probability: float | None
    risk: float | None
    gap_risk: float | None
    is_out_of_sample: bool
    task: str


@dataclass
class TrainOutput:
    status: str
    reason: str | None
    task: str
    metrics: dict = field(default_factory=dict)
    folds: list[Fold] = field(default_factory=list)
    predictions: list[Prediction] = field(default_factory=list)
    regimes: list[str] = field(default_factory=list)
    tuning_timestamps: list = field(default_factory=list)
    models: dict = field(default_factory=dict)
    columns: tuple[str, ...] = ()
    train_end: object = None
    test_start: object = None


def assess_dataset(
    *,
    samples: int,
    symbols: int,
    calendar_days: int,
    regimes: int,
    folds: int,
    config: AiConfig,
) -> str | None:
    reasons = []
    if samples < config.min_samples:
        reasons.append(f"samples {samples} < {config.min_samples}")
    if symbols < config.min_symbols:
        reasons.append(f"symbols {symbols} < {config.min_symbols}")
    if calendar_days < config.min_calendar_days:
        reasons.append(f"calendar days {calendar_days} < {config.min_calendar_days}")
    if regimes < config.min_regimes:
        reasons.append(f"regimes {regimes} < {config.min_regimes}")
    if folds < config.min_folds:
        reasons.append(f"walk-forward folds {folds} < {config.min_folds}")
    return "; ".join(reasons) if reasons else None


def train_samples(
    samples: list[dict],
    *,
    task: str,
    config: AiConfig,
    calendar_days: int,
    regime_names: list[str] | None = None,
) -> TrainOutput:
    columns = DAY_COLUMNS if task == "day" else SWING_COLUMNS
    label_names = sorted({name for sample in samples for name in sample["labels"]})
    if regime_names is None:
        regimes = sorted(
            {RuleRegimeClassifier(config).classify(sample.get("regime_ready") or sample["features"]) for sample in samples}
        )
    else:
        regimes = sorted(set(regime_names))
    if config.embargo_minutes <= 0:
        embargo = timedelta(0)
    elif task == "swing":
        embargo = timedelta(days=1)
    else:
        embargo = timedelta(minutes=config.embargo_minutes)
    folds_by_label = {
        label: _folds_for_label(samples, label, config.min_folds, embargo) for label in label_names
    }
    trainable = {label: folds for label, folds in folds_by_label.items() if len(folds) >= config.min_folds}
    reason = assess_dataset(
        samples=len(samples),
        symbols=len({sample["symbol"] for sample in samples}) if samples else 0,
        calendar_days=calendar_days,
        regimes=len(regimes),
        folds=max((len(folds) for folds in folds_by_label.values()), default=0),
        config=config,
    )
    if reason:
        return TrainOutput(status="REFUSED", reason=reason, task=task, regimes=regimes, columns=columns)
    if not trainable:
        return TrainOutput(
            status="REFUSED",
            reason=f"walk-forward folds 0 < {config.min_folds}",
            task=task,
            regimes=regimes,
            columns=columns,
        )
    try:
        return _fit(samples, task, columns, trainable, config, regimes)
    except Exception as exc:  # noqa: BLE001 - a learner failure must not become an active model
        return TrainOutput(status="FAILED", reason=str(exc), task=task, regimes=regimes, columns=columns)


def _folds_for_label(samples: list[dict], label: str, n_folds: int, embargo: timedelta) -> list[Fold]:
    indexes = [index for index, sample in enumerate(samples) if label in sample["labels"] and _label_end(sample, label) is not None]
    if len(indexes) < n_folds * 3:
        return []
    local = walk_forward(
        [samples[index]["timestamp"] for index in indexes],
        [_label_end(sample, label) for sample in (samples[index] for index in indexes)],
        n_folds=n_folds,
        embargo=embargo,
    )
    return [
        Fold(
            tuple(indexes[index] for index in fold.train),
            tuple(indexes[index] for index in fold.validation),
            tuple(indexes[index] for index in fold.test),
        )
        for fold in local
    ]


def _label_end(sample: dict, label: str):
    ends = sample.get("label_ends") or {}
    return ends.get(label, sample.get("label_end"))


def _fit(samples, task, columns, trainable: dict[str, list[Fold]], config, regimes) -> TrainOutput:
    models = {}
    metrics: dict[str, dict] = {}
    predictions: list[Prediction] = []
    tuning = []
    train_ends = []
    test_starts = []
    stored_folds: list[Fold] = []
    for label, folds in trainable.items():
        last = folds[-1]
        stored_folds = folds
        tuning.extend(samples[index]["timestamp"] for index in (*last.train, *last.validation))
        train_ends.append(samples[last.train[-1]]["timestamp"])
        test_starts.append(samples[last.test[0]]["timestamp"])
        trained = _fit_label(samples, columns, label, last, config, task)
        models[label] = trained["models"]
        metrics[label] = trained["metrics"]
        if trained["metrics"]["test_samples"] < config.min_test_samples:
            continue
        predictions.extend(trained["predictions"])
    if not predictions:
        return TrainOutput(
            status="REFUSED",
            reason=f"test samples are below {config.min_test_samples}",
            task=task,
            metrics=metrics,
            folds=stored_folds,
            regimes=regimes,
            columns=columns,
        )
    test_start = min(item.timestamp for item in predictions)
    return TrainOutput(
        status="SUCCEEDED",
        reason=None,
        task=task,
        metrics=metrics,
        folds=stored_folds,
        predictions=predictions,
        regimes=regimes,
        tuning_timestamps=[item for item in tuning if item < test_start],
        models=models,
        columns=columns,
        train_end=max(item for item in train_ends if item < test_start),
        test_start=test_start,
    )


def _fit_label(samples, columns, label, fold: Fold, config: AiConfig, task: str) -> dict:
    grid = ({"num_leaves": 7}, {"num_leaves": 15})
    best = None
    best_rmse = None
    for params in grid:
        model = fit_booster(_xy(samples, columns, label, fold.train), params, config, objective="regression")
        validation = predict_booster(model, _x(samples, columns, fold.validation))
        rmse = _rmse([samples[index]["labels"][label] for index in fold.validation], validation)
        if best_rmse is None or (rmse is not None and rmse < best_rmse):
            best_rmse = rmse
            best = params
    if best is None or best_rmse is None:
        raise RuntimeError(f"validation metric is unavailable for {label}")
    chosen = fit_booster(_xy(samples, columns, label, fold.train), best, config, objective="regression")
    test_x = _x(samples, columns, fold.test)
    expected = predict_booster(chosen, test_x)
    test_y = [samples[index]["labels"][label] for index in fold.test]
    rmse = _rmse(test_y, expected)
    direction = _direction(test_y, expected)
    classes = {1 if samples[index]["labels"][label] > 0 else 0 for index in fold.train}
    probability = [None] * len(fold.test)
    if len(classes) == 2:
        classifier = fit_booster(_xy(samples, columns, label, fold.train, binary=True), best, config, objective="binary")
        probability = predict_booster(classifier, test_x)
    else:
        classifier = None
    risk = best_rmse
    predictions = []
    for offset, index in enumerate(fold.test):
        sample = samples[index]
        predictions.append(
            Prediction(
                symbol=sample["symbol"],
                timestamp=sample["timestamp"],
                available_at=sample["available_at"],
                expires_at=_expires(sample, label),
                horizon=label,
                expected_return=expected[offset],
                up_probability=None if probability[offset] is None else float(probability[offset]),
                risk=risk,
                gap_risk=sample.get("gap_risk"),
                is_out_of_sample=True,
                task=task,
            )
        )
    return {
        "models": {"return": chosen, "up": classifier, "params": best, "columns": columns},
        "metrics": {
            "validation_rmse": best_rmse,
            "test_rmse": rmse,
            "directional_accuracy": direction,
            "test_samples": len(fold.test),
        },
        "predictions": predictions,
    }


def fit_booster(dataset: tuple[list[list[float]], list[float]], params: dict, config: AiConfig, *, objective: str):
    if config.learner == "catboost":
        raise RuntimeError("CatBoost comparison backend is not enabled")
    import lightgbm as lgb

    import numpy as np

    features, target = dataset
    if not features:
        raise RuntimeError("learner received no rows")
    train = lgb.Dataset(np.asarray(features, dtype=float), label=np.asarray(target, dtype=float))
    booster_params = {
        "objective": objective,
        "learning_rate": 0.1,
        "num_leaves": int(params.get("num_leaves", 7)),
        "min_data_in_leaf": 1,
        "verbose": -1,
        "seed": config.seed,
        "deterministic": True,
        "force_row_wise": True,
        "num_threads": 1,
        "feature_fraction": 1.0,
        "bagging_fraction": 1.0,
    }
    return lgb.train(booster_params, train, num_boost_round=20)


def predict_booster(model, rows: list[list[float]]) -> list[float]:
    if model is None or not rows:
        return []
    import numpy as np

    values = model.predict(np.asarray(rows, dtype=float))
    return [float(value) for value in values]


def save_boosters(directory, models: dict) -> None:
    from pathlib import Path

    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    for label, bundle in models.items():
        bundle["return"].save_model(str(root / f"{label}_return.txt"))
        if bundle["up"] is not None:
            bundle["up"].save_model(str(root / f"{label}_up.txt"))


def load_boosters(directory, labels: list[str]) -> dict:
    import lightgbm as lgb
    from pathlib import Path

    root = Path(directory)
    loaded = {}
    for label in labels:
        return_path = root / f"{label}_return.txt"
        up_path = root / f"{label}_up.txt"
        loaded[label] = {
            "return": lgb.Booster(model_file=str(return_path)),
            "up": lgb.Booster(model_file=str(up_path)) if up_path.exists() else None,
        }
    return loaded


def _expires(sample: dict, label: str):
    text = label.split("_")[-1]
    if text.endswith("m"):
        return sample["available_at"] + timedelta(minutes=int(text[:-1]))
    return sample["label_end"]


def _xy(samples, columns, label, indexes, binary: bool = False):
    features = []
    target = []
    for index in indexes:
        value = samples[index]["labels"][label]
        features.append([float(samples[index]["features"][name]) for name in columns])
        target.append(1.0 if binary and value > 0 else 0.0 if binary else float(value))
    return features, target


def _x(samples, columns, indexes):
    return [[float(samples[index]["features"][name]) for name in columns] for index in indexes]


def _rmse(actual: list[float], predicted: list[float]) -> float | None:
    if not actual or len(actual) != len(predicted):
        return None
    return math.sqrt(sum((left - right) ** 2 for left, right in zip(actual, predicted, strict=True)) / len(actual))


def _direction(actual: list[float], predicted: list[float]) -> float | None:
    if not actual or len(actual) != len(predicted):
        return None
    hits = sum(1 for left, right in zip(actual, predicted, strict=True) if (left > 0) == (right > 0))
    return hits / len(actual)
