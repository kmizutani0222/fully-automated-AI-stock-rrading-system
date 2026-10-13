from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.data.repository import ensure_instrument, get_instrument, list_instruments
from app.db.session import get_session
from app.schemas.instrument import InstrumentIn, InstrumentOut

router = APIRouter()


def _to_out(row) -> InstrumentOut:
    return InstrumentOut(
        market=row.market,
        symbol=row.symbol,
        name=row.name,
        exchange=row.exchange,
        currency=row.currency,
        active=row.active,
    )


@router.get("/instruments", response_model=list[InstrumentOut])
def instruments(market: str | None = None, db: Session = Depends(get_session)) -> list[InstrumentOut]:
    return [_to_out(row) for row in list_instruments(db, market=market)]


@router.post("/instruments", response_model=InstrumentOut, status_code=201)
def create_instrument(payload: InstrumentIn, db: Session = Depends(get_session)) -> InstrumentOut:
    if get_instrument(db, payload.market, payload.symbol) is not None:
        raise HTTPException(status_code=409, detail="instrument already exists")
    try:
        row = ensure_instrument(
            db,
            payload.market,
            payload.symbol,
            name=payload.name,
            exchange=payload.exchange,
            currency=payload.currency,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    db.refresh(row)
    return _to_out(row)
