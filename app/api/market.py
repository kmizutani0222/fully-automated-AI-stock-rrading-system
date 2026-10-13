from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.constants import TIMEFRAME
from app.data.bar import NormalizedBar
from app.data.quality import inspect_bars
from app.data.repository import get_instrument, load_bars
from app.db.session import get_session
from app.schemas.market import BarOut, GapOut, QualityOut

router = APIRouter()


def _bar_out(symbol: str, market: str, row) -> BarOut:
    return BarOut(
        market=market,
        symbol=symbol,
        timeframe=row.timeframe,
        timestamp=row.timestamp,
        open=float(row.open),
        high=float(row.high),
        low=float(row.low),
        close=float(row.close),
        volume=None if row.volume is None else float(row.volume),
        vwap=None if row.vwap is None else float(row.vwap),
        trade_count=row.trade_count,
        provider=row.provider,
    )


@router.get("/bars/{symbol}", response_model=list[BarOut])
def bars(
    symbol: str,
    market: str = "US",
    timeframe: str = TIMEFRAME,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = Query(default=500, le=5000),
    db: Session = Depends(get_session),
) -> list[BarOut]:
    if timeframe != TIMEFRAME:
        raise HTTPException(status_code=400, detail=f"Phase 1 only serves {TIMEFRAME}")
    instrument = get_instrument(db, market, symbol)
    if instrument is None:
        raise HTTPException(status_code=404, detail="instrument not found")
    rows = load_bars(db, instrument.id, timeframe, start=start, end=end)
    return [_bar_out(instrument.symbol, instrument.market, row) for row in rows[-limit:]]


@router.get("/data-quality/{symbol}", response_model=QualityOut)
def data_quality(
    symbol: str,
    market: str = "US",
    timeframe: str = TIMEFRAME,
    db: Session = Depends(get_session),
) -> QualityOut:
    instrument = get_instrument(db, market, symbol)
    if instrument is None:
        raise HTTPException(status_code=404, detail="instrument not found")
    rows = load_bars(db, instrument.id, timeframe)
    normalized = [
        NormalizedBar(
            market=instrument.market,
            symbol=instrument.symbol,
            timestamp=row.timestamp,
            timeframe=row.timeframe,
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            volume=row.volume,
            vwap=row.vwap,
            trade_count=row.trade_count,
            provider=row.provider,
            is_adjusted=row.is_adjusted,
        )
        for row in rows
    ]
    report = inspect_bars(normalized)
    return QualityOut(
        symbol=instrument.symbol,
        rows=report.valid_rows,
        gap_count=len(report.gaps),
        gaps=[
            GapOut(symbol=gap.symbol, previous=gap.previous, current=gap.current, minutes=gap.minutes)
            for gap in report.gaps[:50]
        ],
    )
