from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.ai.config import AiConfig, load_ai_config
from app.ai.features import compute_features, completed_daily_bars
from app.ai.labels import day_samples, session_dates, swing_samples
from app.ai.regimes import RuleRegimeClassifier
from app.ai.registry import ModelRecord, activation_block
from app.ai.selector import Forecast, select_strategy
from app.ai.strategy import ReplayStrategy
from app.ai.training import TrainOutput, save_boosters, train_samples
from app.backtest.engine import run_backtest
from app.backtest.service import queue_backtest
from app.data.repository import get_instrument, load_bars
from app.db.session import SessionLocal
from app.models.ai import (
    FeatureSnapshot,
    MarketRegime,
    ModelMetric,
    ModelVersion,
    PredictionRecord,
    StrategyDecision,
    TrainingRun,
    WalkForwardRun,
)
from app.models.trading import BacktestRun
from app.trading.config import TradingConfig, load_trading_config
from app.trading.strategies import PriceBar, build_strategy

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODEL_ROOT = PROJECT_ROOT / "data" / "models"


def queue_training(
    session: Session,
    *,
    task: str,
    market: str,
    symbols: list[str],
    start: datetime,
    end: datetime,
    provider: str,
    index_symbol: str | None,
    parameters: dict | None,
) -> TrainingRun:
    if task not in {"day", "swing"}:
        raise ValueError("task must be day or swing")
    run = TrainingRun(
        task=task,
        status="PENDING",
        market=market.upper(),
        symbols=[symbol.upper() for symbol in symbols],
        provider=provider,
        start_at=start,
        end_at=end,
        parameters={"index_symbol": index_symbol.upper() if index_symbol else None, **(parameters or {})},
        reason=None,
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def execute_training(run_id: int) -> None:
    with SessionLocal() as session:
        run = session.get(TrainingRun, run_id)
        if run is None:
            return
        run.status = "RUNNING"
        run.started_at = datetime.now(timezone.utc)
        session.commit()
        try:
            _execute_training(session, run)
        except Exception as exc:  # noqa: BLE001 - the job has to record the failure
            session.rollback()
            failed = session.get(TrainingRun, run_id)
            if failed is not None:
                failed.status = "FAILED"
                failed.reason = str(exc)
                failed.finished_at = datetime.now(timezone.utc)
                session.commit()


def _execute_training(session: Session, run: TrainingRun) -> None:
    config = load_ai_config().with_overrides(run.parameters)
    symbols = list(run.symbols)
    bars = _load(session, run.market, symbols, run.provider, run.start_at, run.end_at)
    index_symbol = run.parameters.get("index_symbol")
    index_bars = []
    if index_symbol:
        index_bars = bars.get(index_symbol) or _load(session, run.market, [index_symbol], run.provider, run.start_at, run.end_at).get(index_symbol, [])
    if not bars or any(not rows for rows in bars.values()):
        _finish(session, run, "REFUSED", "no closed bars for the requested symbols")
        return
    classifier = RuleRegimeClassifier(config)
    samples = []
    regime_names: list[str] = []
    calendar_days: set = set()
    index_symbol = run.parameters.get("index_symbol")
    index_daily = completed_daily_bars(index_bars, run.market) if index_bars else []
    for symbol, rows in bars.items():
        instrument = get_instrument(session, run.market, symbol)
        if instrument is None:
            _finish(session, run, "REFUSED", f"instrument {run.market}:{symbol} is not registered")
            return
        features = compute_features(rows, run.market)
        _store_features(session, instrument.id, features, config.feature_version, classifier)
        regime_names.extend(classifier.classify(row.values) for row in features)
        calendar_days.update(session_dates(rows, run.market))
        if run.task == "day":
            samples.extend(day_samples(features, rows, run.market, config.day_horizons_bars))
        else:
            if not index_symbol:
                _finish(session, run, "REFUSED", "swing training requires an index symbol for relative strength")
                return
            samples.extend(swing_samples(completed_daily_bars(rows, run.market), index_daily, run.market, config.swing_horizons_days))
    samples.sort(key=lambda sample: (sample["timestamp"], sample["symbol"]))
    output = train_samples(
        samples,
        task=run.task,
        config=config,
        calendar_days=len(calendar_days),
        regime_names=regime_names,
    )
    if output.status != "SUCCEEDED":
        _finish(session, run, output.status, output.reason)
        return
    version = _store_model(session, run, output, config)
    run.model_version_id = version.id
    run.status = "SUCCEEDED"
    run.reason = None
    run.finished_at = datetime.now(timezone.utc)
    session.commit()


def activate_model(session: Session, model_id: int) -> ModelVersion:
    model = session.get(ModelVersion, model_id)
    if model is None:
        raise KeyError(model_id)
    metrics: dict[str, dict] = {}
    for row in session.scalars(select(ModelMetric).where(ModelMetric.model_version_id == model.id)).all():
        metrics.setdefault(row.label, {})[row.metric_name] = None if row.metric_value is None else float(row.metric_value)
        if row.metric_name == "test_rmse":
            metrics[row.label]["test_samples"] = row.sample_count or 0
    record = ModelRecord(
        version=model.version,
        task=model.task,
        status="TRAINED" if model.status == "TRAINED" else model.status,
        test_metrics=metrics,
        path=model.artifact_path,
    )
    reason = activation_block(record)
    if reason:
        raise ValueError(reason)
    others = session.scalars(select(ModelVersion).where(ModelVersion.task == model.task, ModelVersion.status == "ACTIVE")).all()
    for other in others:
        if other.id != model.id:
            other.status = "INACTIVE"
    model.status = "ACTIVE"
    model.activated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(model)
    return model


def queue_ai_backtests(
    session: Session,
    *,
    market: str,
    symbols: list[str],
    start: datetime,
    end: datetime,
    provider: str,
    initial_cash: dict[str, str] | None,
    parameters: dict | None,
) -> list[BacktestRun]:
    runs = []
    for name in ("ema_cross", "rsi_reversion", "buy_hold", "day_ai", "swing_ai", "ai_selector"):
        if name in {"day_ai", "swing_ai", "ai_selector"}:
            run = BacktestRun(
                strategy_name=name,
                strategy_version="v1",
                status="PENDING",
                market=market.upper(),
                timeframe="5m",
                symbols=[symbol.upper() for symbol in symbols],
                start_at=start,
                end_at=end,
                provider=provider,
                parameters=parameters or {},
                initial_cash=initial_cash or {"JPY": "1000000", "USD": "0"},
            )
            session.add(run)
        else:
            run = queue_backtest(
                session,
                strategy_name=name,
                market=market,
                symbols=symbols,
                start=start,
                end=end,
                provider=provider,
                initial_cash=initial_cash,
                parameters=parameters,
            )
        runs.append(run)
    session.commit()
    for run in runs:
        session.refresh(run)
    return runs


def execute_ai_backtests(run_ids: list[int]) -> None:
    from app.backtest.service import execute_backtest_run

    for run_id in run_ids:
        with SessionLocal() as session:
            run = session.get(BacktestRun, run_id)
            if run is None:
                continue
            if run.strategy_name in {"ema_cross", "rsi_reversion", "buy_hold"}:
                execute_backtest_run(run_id)
                continue
            _execute_ai_run(session, run)


def _execute_ai_run(session: Session, run: BacktestRun) -> None:
    run.status = "RUNNING"
    run.started_at = datetime.now(timezone.utc)
    session.commit()
    predictions = list(
        session.scalars(select(PredictionRecord).where(PredictionRecord.is_out_of_sample.is_(True)).order_by(PredictionRecord.id)).all()
    )
    relevant = [row for row in predictions if row.horizon.startswith("return_")]
    if run.strategy_name == "day_ai":
        relevant = [row for row in relevant if row.horizon.endswith("m")]
    elif run.strategy_name == "swing_ai":
        relevant = [row for row in relevant if row.horizon.endswith("d")]
    if not relevant:
        run.status = "FAILED"
        run.error = "no out-of-sample predictions are available"
        run.metrics = None
        run.finished_at = datetime.now(timezone.utc)
        session.commit()
        return
    try:
        output = _replay(session, run, relevant)
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        failed = session.get(BacktestRun, run.id)
        if failed is not None:
            failed.status = "FAILED"
            failed.error = str(exc)
            failed.finished_at = datetime.now(timezone.utc)
            session.commit()
        return
    run = session.get(BacktestRun, run.id)
    run.status = "SUCCEEDED"
    run.metrics = output.metrics
    run.error = None
    run.finished_at = datetime.now(timezone.utc)
    session.commit()


def _replay(session: Session, run: BacktestRun, predictions: list[PredictionRecord]):
    config = load_trading_config()
    ai_config = load_ai_config()
    bars: dict[str, list[PriceBar]] = {}
    currencies: dict[str, str] = {}
    instruments = {}
    for symbol in run.symbols:
        instrument = get_instrument(session, run.market, symbol)
        if instrument is None:
            raise RuntimeError(f"instrument {run.market}:{symbol} is not registered")
        instruments[symbol] = instrument
        rows = load_bars(session, instrument.id, "5m", provider=run.provider, start=run.start_at, end=run.end_at)
        bars[symbol] = [
            PriceBar(symbol, row.timestamp, Decimal(row.open), Decimal(row.high), Decimal(row.low), Decimal(row.close), None if row.volume is None else Decimal(row.volume))
            for row in rows
        ]
        currencies[symbol] = instrument.currency
    cost = float(config.spread_pct + (config.slippage_pct * 2) + (config.commission_pct * 2))
    decisions = {}
    for symbol, rows in bars.items():
        symbol_predictions = [row for row in predictions if row.instrument_id == instruments[symbol].id]
        for bar in rows:
            day, swing = _forecasts(symbol, bar.timestamp, symbol_predictions)
            if run.strategy_name == "day_ai":
                swing = []
            elif run.strategy_name == "swing_ai":
                day = []
            decision = select_strategy(symbol, day, swing, cost=cost, now=bar.timestamp)
            decisions[(symbol, bar.timestamp)] = decision
            session.add(
                StrategyDecision(
                    instrument_id=instruments[symbol].id,
                    backtest_run_id=run.id,
                    timestamp=bar.timestamp,
                    choice=decision.choice,
                    reason=decision.reason,
                    score=None if decision.score is None else Decimal(str(decision.score)),
                    details={"managed_styles": decision.managed_styles},
                )
            )
    cash = {key: Decimal(str(value)) for key, value in run.initial_cash.items()}
    strategy = ReplayStrategy(decisions)
    if run.strategy_name in {"ema_cross", "rsi_reversion", "buy_hold"}:
        strategy = build_strategy(run.strategy_name, config)
    return run_backtest(bars, currencies, strategy, _trading(config, ai_config), run.market, cash)


def _horizon_steps(horizon: str) -> int:
    stem = horizon.removeprefix("return_")
    if stem.endswith("m"):
        return max(int(stem[:-1]) // 5, 1)
    if stem.endswith("d"):
        return max(int(stem[:-1]), 1)
    return 1


def _forecasts(symbol: str, timestamp, rows: list[PredictionRecord]) -> tuple[list[Forecast], list[Forecast]]:
    day: list[Forecast] = []
    swing: list[Forecast] = []
    for row in rows:
        if row.predicted_at != timestamp:
            continue
        forecast = Forecast(
            symbol=symbol,
            style="DAY" if row.horizon.endswith("m") else "SWING",
            horizon_steps=_horizon_steps(row.horizon),
            expected_return=None if row.expected_return is None else float(row.expected_return),
            up_probability=None if row.up_probability is None else float(row.up_probability),
            risk=None if row.risk is None else float(row.risk),
            gap_risk=None if row.gap_risk is None else float(row.gap_risk),
            available_at=row.available_at,
            expires_at=row.expires_at,
            is_out_of_sample=row.is_out_of_sample,
        )
        if forecast.style == "DAY":
            day.append(forecast)
        else:
            swing.append(forecast)
    return day, swing


def _trading(config: TradingConfig, ai_config: AiConfig) -> TradingConfig:
    return config


def _store_features(session, instrument_id, features, version, classifier) -> None:
    session.execute(delete(FeatureSnapshot).where(FeatureSnapshot.instrument_id == instrument_id, FeatureSnapshot.feature_version == version))
    session.execute(delete(MarketRegime).where(MarketRegime.instrument_id == instrument_id, MarketRegime.method == classifier.method))
    for row in features:
        session.add(
            FeatureSnapshot(
                instrument_id=instrument_id,
                timestamp=row.timestamp,
                available_at=row.available_at,
                feature_version=version,
                values=row.values,
            )
        )
        session.add(
            MarketRegime(
                instrument_id=instrument_id,
                timestamp=row.timestamp,
                available_at=row.available_at,
                regime=classifier.classify(row.values),
                method=classifier.method,
                details={},
            )
        )
    session.flush()


def _store_model(session, run: TrainingRun, output: TrainOutput, config: AiConfig) -> ModelVersion:
    version_name = f"{run.task}-{run.id}"
    path = MODEL_ROOT / version_name
    save_boosters(path, output.models)
    model = ModelVersion(
        task=run.task,
        version=version_name,
        family=config.learner,
        status="TRAINED",
        feature_version=config.feature_version,
        train_start=run.start_at,
        train_end=output.train_end,
        test_start=output.test_start,
        test_end=output.predictions[-1].timestamp if output.predictions else None,
        parameters={"columns": list(output.columns)},
        artifact_path=str(path),
    )
    session.add(model)
    session.flush()
    for label, metrics in output.metrics.items():
        for name, value in metrics.items():
            if name == "test_samples":
                continue
            session.add(
                ModelMetric(
                    model_version_id=model.id,
                    label=label,
                    metric_name=name,
                    metric_value=None if value is None else Decimal(str(value)),
                    sample_count=metrics.get("test_samples"),
                )
            )
    for fold_index, fold in enumerate(output.folds):
        session.add(
            WalkForwardRun(
                training_run_id=run.id,
                fold=fold_index,
                train_start=None,
                train_end=output.train_end if fold_index == len(output.folds) - 1 else None,
                test_start=output.test_start if fold_index == len(output.folds) - 1 else None,
                test_end=None,
                status="SUCCEEDED",
                metrics={"train_rows": len(fold.train), "validation_rows": len(fold.validation), "test_rows": len(fold.test)},
            )
        )
    instrument_ids = {symbol: get_instrument(session, run.market, symbol).id for symbol in run.symbols}
    for prediction in output.predictions:
        if prediction.symbol not in instrument_ids:
            continue
        session.add(
            PredictionRecord(
                model_version_id=model.id,
                instrument_id=instrument_ids[prediction.symbol],
                predicted_at=prediction.timestamp,
                available_at=prediction.available_at,
                expires_at=prediction.expires_at,
                horizon=prediction.horizon,
                expected_return=None if prediction.expected_return is None else Decimal(str(prediction.expected_return)),
                up_probability=None if prediction.up_probability is None else Decimal(str(prediction.up_probability)),
                risk=None if prediction.risk is None else Decimal(str(prediction.risk)),
                gap_risk=None if prediction.gap_risk is None else Decimal(str(prediction.gap_risk)),
                is_out_of_sample=prediction.is_out_of_sample,
            )
        )
    session.flush()
    return model


def _finish(session, run: TrainingRun, status: str, reason: str | None) -> None:
    run.status = status
    run.reason = reason
    run.finished_at = datetime.now(timezone.utc)
    session.commit()


def _load(session, market, symbols, provider, start, end) -> dict[str, list[PriceBar]]:
    loaded = {}
    for symbol in symbols:
        instrument = get_instrument(session, market, symbol)
        if instrument is None:
            continue
        rows = load_bars(session, instrument.id, "5m", provider=provider, start=start, end=end)
        loaded[symbol] = [
            PriceBar(
                symbol,
                row.timestamp,
                Decimal(row.open),
                Decimal(row.high),
                Decimal(row.low),
                Decimal(row.close),
                None if row.volume is None else Decimal(row.volume),
            )
            for row in rows
        ]
    return loaded
