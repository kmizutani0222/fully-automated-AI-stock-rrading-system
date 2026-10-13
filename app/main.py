from __future__ import annotations

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.imports import router as import_router
from app.api.instruments import router as instrument_router
from app.api.market import router as market_router
from app.api.v1_routes import router as v1_router
from app.core.config import get_settings
from app.core.logging import setup_logging

settings = get_settings()
setup_logging(settings.log_level)

app = FastAPI(title="AI Trading Platform", version="0.1.0")
app.include_router(health_router)
app.include_router(instrument_router)
app.include_router(market_router)
app.include_router(import_router)
app.include_router(v1_router)


@app.get("/")
def root() -> dict:
    return {
        "service": "AI Trading Platform",
        "phase": 2,
        "trading_mode": settings.trading_mode,
        "live_trading_enabled": False,
        "docs": "/docs",
    }
