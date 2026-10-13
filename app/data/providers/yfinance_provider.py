from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pandas as pd

from app.core.constants import TIMEFRAME
from app.data.bar import NormalizedBar
from app.data.normalizer import parse_decimal, parse_timestamp, require_timeframe
from app.data.providers.base import MarketDataProvider, ProviderError

YFINANCE_LOOKBACK_DAYS = 59


def frame_from_yfinance(frame: pd.DataFrame, symbol: str) -> list[NormalizedBar]:
    if frame is None or frame.empty:
        return []
    working = frame.copy()
    if isinstance(working.columns, pd.MultiIndex):
        working.columns = working.columns.get_level_values(0)
    working = working.reset_index()
    working.columns = [str(column).strip().lower().replace(" ", "_") for column in working.columns]
    time_column = next((name for name in ("datetime", "date", "index") if name in working.columns), working.columns[0])
    bars: list[NormalizedBar] = []
    for _, row in working.iterrows():
        try:
            open_ = parse_decimal(row["open"])
            high = parse_decimal(row["high"])
            low = parse_decimal(row["low"])
            close = parse_decimal(row["close"])
            if None in {open_, high, low, close}:
                continue
            volume = parse_decimal(row["volume"]) if "volume" in working.columns else None
            bars.append(
                NormalizedBar(
                    market="US",
                    symbol=symbol.upper(),
                    timestamp=parse_timestamp(row[time_column], "UTC"),
                    timeframe=TIMEFRAME,
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                    provider="yfinance",
                    is_adjusted=False,
                )
            )
        except (TypeError, ValueError, KeyError):
            continue
    return bars


class YFinanceUsProvider(MarketDataProvider):
    """Free US 5-minute bars. Yahoo only serves about 60 days of this interval."""

    name = "yfinance"

    async def get_bars(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> list[NormalizedBar]:
        require_timeframe(timeframe)
        start_utc = start.astimezone(timezone.utc)
        end_utc = end.astimezone(timezone.utc)
        try:
            frame = await asyncio.to_thread(_download, symbol, start_utc, end_utc)
        except Exception as exc:  # noqa: BLE001 - surface the vendor failure without inventing bars
            raise ProviderError(f"yfinance request failed for {symbol}: {exc}") from exc
        bars = frame_from_yfinance(frame, symbol)
        return [bar for bar in bars if start_utc <= bar.timestamp < end_utc]

    async def get_latest_bar(self, symbol: str, timeframe: str) -> NormalizedBar | None:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=5)
        bars = await self.get_bars(symbol, timeframe, start, end)
        return bars[-1] if bars else None


def _download(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    import yfinance as yf

    earliest = end - timedelta(days=YFINANCE_LOOKBACK_DAYS)
    request_start = max(start, earliest)
    data = yf.download(
        tickers=symbol,
        interval="5m",
        start=request_start.strftime("%Y-%m-%d"),
        end=(end + timedelta(days=1)).strftime("%Y-%m-%d"),
        auto_adjust=False,
        progress=False,
        threads=False,
    )
    if data is None:
        return pd.DataFrame()
    return data
