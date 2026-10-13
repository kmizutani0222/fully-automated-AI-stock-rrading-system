from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from app.core.constants import TIMEFRAME
from app.data.bar import NormalizedBar
from app.data.normalizer import parse_decimal, parse_timestamp, require_market


@dataclass
class CsvLoadResult:
    bars: list[NormalizedBar] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def load_csv(path: Path, source_timezone: str = "UTC", provider: str = "csv") -> CsvLoadResult:
    try:
        frame = pd.read_csv(path, encoding="utf-8-sig")
    except pd.errors.EmptyDataError:
        return CsvLoadResult(errors=["empty csv"])
    frame.columns = [str(column).strip().lower() for column in frame.columns]
    required = {"market", "symbol", "timestamp", "open", "high", "low", "close"}
    missing = required - set(frame.columns)
    if missing:
        return CsvLoadResult(errors=[f"missing columns: {', '.join(sorted(missing))}"])

    result = CsvLoadResult()
    for index, row in frame.iterrows():
        line = int(index) + 2
        try:
            market = require_market(str(row["market"]))
            symbol = str(row["symbol"]).strip().upper()
            if not symbol:
                raise ValueError("symbol is empty")
            timeframe = TIMEFRAME
            if "timeframe" in frame.columns and pd.notna(row["timeframe"]):
                timeframe = str(row["timeframe"]).strip()
            prices = {
                name: parse_decimal(row[name])
                for name in ("open", "high", "low", "close")
            }
            if any(price is None for price in prices.values()):
                raise ValueError("open, high, low, and close are required")
            volume = parse_decimal(row["volume"]) if "volume" in frame.columns else None
            vwap = parse_decimal(row["vwap"]) if "vwap" in frame.columns else None
            trade_count = None
            if "trade_count" in frame.columns and pd.notna(row["trade_count"]) and str(row["trade_count"]).strip():
                trade_count = int(row["trade_count"])
            result.bars.append(
                NormalizedBar(
                    market=market,
                    symbol=symbol,
                    timestamp=parse_timestamp(row["timestamp"], source_timezone),
                    timeframe=timeframe,
                    open=prices["open"],
                    high=prices["high"],
                    low=prices["low"],
                    close=prices["close"],
                    volume=volume,
                    vwap=vwap,
                    trade_count=trade_count,
                    provider=provider,
                )
            )
        except (TypeError, ValueError) as exc:
            result.errors.append(f"line {line}: {exc}")
    return result
