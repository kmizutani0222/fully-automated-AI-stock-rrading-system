from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import engine

router = APIRouter()


@router.get("/health")
def health():
    settings = get_settings()
    database = "ok"
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - report the failure instead of crashing the health check
        database = str(exc)
    body = {
        "status": "ok" if database == "ok" else "degraded",
        "database": database,
        "trading_mode": settings.trading_mode,
        "live_trading_enabled": False,
        "trading_enabled": settings.trading_enabled,
    }
    if database != "ok":
        return JSONResponse(status_code=503, content=body)
    return body
