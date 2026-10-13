from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.service import activate_model, execute_ai_backtests, execute_training, queue_ai_backtests, queue_training
from app.db.session import get_session
from app.models.ai import MarketRegime, ModelVersion, PredictionRecord, StrategyDecision, TrainingRun
from app.schemas.ai import AiBacktestIn, TrainIn

router = APIRouter(prefix="/api/v1/ai")


@router.get("/models")
def models(db: Session = Depends(get_session)) -> list[dict]:
    rows = db.scalars(select(ModelVersion).order_by(ModelVersion.id.desc())).all()
    return [
        {
            "id": row.id,
            "task": row.task,
            "version": row.version,
            "family": row.family,
            "status": row.status,
            "feature_version": row.feature_version,
            "train_end": row.train_end,
            "test_start": row.test_start,
        }
        for row in rows
    ]


@router.post("/train", status_code=202)
def train(payload: TrainIn, background: BackgroundTasks, db: Session = Depends(get_session)) -> dict:
    try:
        run = queue_training(
            db,
            task=payload.task,
            market=payload.market,
            symbols=payload.symbols,
            start=payload.start,
            end=payload.end,
            provider=payload.provider,
            index_symbol=payload.index_symbol,
            parameters=payload.parameters,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    background.add_task(execute_training, run.id)
    return {"id": run.id, "status": run.status}


@router.get("/training-runs")
def training_runs(db: Session = Depends(get_session)) -> list[dict]:
    rows = db.scalars(select(TrainingRun).order_by(TrainingRun.id.desc())).all()
    return [_run(row) for row in rows]


@router.get("/training-runs/{run_id}")
def training_run(run_id: int, db: Session = Depends(get_session)) -> dict:
    row = db.get(TrainingRun, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="training run not found")
    return _run(row)


@router.post("/models/{model_id}/activate")
def activate(model_id: int, db: Session = Depends(get_session)) -> dict:
    try:
        model = activate_model(db, model_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="model not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"id": model.id, "status": model.status}


@router.get("/predictions")
def predictions(db: Session = Depends(get_session)) -> list[dict]:
    rows = db.scalars(select(PredictionRecord).order_by(PredictionRecord.id.desc()).limit(200)).all()
    return [
        {
            "id": row.id,
            "model_version_id": row.model_version_id,
            "horizon": row.horizon,
            "predicted_at": row.predicted_at,
            "expires_at": row.expires_at,
            "expected_return": None if row.expected_return is None else format(row.expected_return, "f"),
            "up_probability": None if row.up_probability is None else format(row.up_probability, "f"),
            "is_out_of_sample": row.is_out_of_sample,
        }
        for row in rows
    ]


@router.get("/regimes")
def regimes(db: Session = Depends(get_session)) -> list[dict]:
    rows = db.scalars(select(MarketRegime).order_by(MarketRegime.id.desc()).limit(200)).all()
    return [{"id": row.id, "timestamp": row.timestamp, "regime": row.regime, "method": row.method} for row in rows]


@router.get("/decisions")
def decisions(db: Session = Depends(get_session)) -> list[dict]:
    rows = db.scalars(select(StrategyDecision).order_by(StrategyDecision.id.desc()).limit(200)).all()
    return [
        {
            "id": row.id,
            "timestamp": row.timestamp,
            "choice": row.choice,
            "reason": row.reason,
            "score": None if row.score is None else format(row.score, "f"),
        }
        for row in rows
    ]


@router.post("/backtests", status_code=202)
def ai_backtests(payload: AiBacktestIn, background: BackgroundTasks, db: Session = Depends(get_session)) -> dict:
    runs = queue_ai_backtests(
        db,
        market=payload.market,
        symbols=payload.symbols,
        start=payload.start,
        end=payload.end,
        provider=payload.provider,
        initial_cash=payload.initial_cash,
        parameters=payload.parameters,
    )
    background.add_task(execute_ai_backtests, [run.id for run in runs])
    return {"runs": [{"id": run.id, "strategy": run.strategy_name, "status": run.status} for run in runs]}


def _run(row: TrainingRun) -> dict:
    return {
        "id": row.id,
        "task": row.task,
        "status": row.status,
        "market": row.market,
        "symbols": list(row.symbols),
        "reason": row.reason,
        "model_version_id": row.model_version_id,
    }
