from __future__ import annotations

from pathlib import Path

from app.core.config import Settings
from app.data.providers.base import MarketDataProvider
from app.data.providers.csv_provider import CsvProvider
from app.data.providers.mock import MockProvider
from app.data.providers.yfinance_provider import YFinanceUsProvider


def build_provider(settings: Settings, csv_path: Path | None = None) -> MarketDataProvider:
    name = settings.market_data_provider.strip().lower()
    if name == "mock":
        return MockProvider()
    if name == "csv":
        if csv_path is None:
            raise ValueError("CSV provider requires a file path")
        return CsvProvider(csv_path, settings.csv_timezone)
    if name == "yfinance":
        return YFinanceUsProvider()
    raise ValueError(f"Unknown market data provider: {settings.market_data_provider}")
