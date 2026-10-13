from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.data.csv_loader import load_csv
from app.data.repository import save_bars
from app.db.session import get_session
from app.schemas.market import ImportOut

router = APIRouter()


@router.post("/imports/csv", response_model=ImportOut)
async def import_csv(
    file: UploadFile = File(...),
    timezone_name: str = Form(default="UTC"),
    db: Session = Depends(get_session),
) -> ImportOut:
    settings = get_settings()
    payload = await file.read()
    with NamedTemporaryFile(suffix=".csv") as handle:
        handle.write(payload)
        handle.flush()
        loaded = load_csv(Path(handle.name), timezone_name or settings.csv_timezone)
    result = save_bars(db, loaded.bars)
    return ImportOut(
        saved=result.saved,
        rejected=len(result.rejected),
        duplicate_keys=len(result.quality.duplicate_keys),
        gap_count=len(result.quality.gaps),
        dropped_open_bars=result.quality.dropped_open_bars,
        errors=loaded.errors + ["; ".join(item.reasons) for item in result.rejected[:20]],
    )
