#!/usr/bin/env python
"""Fetch US 5-minute bars. A failed request does not insert mock prices."""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone

from app.core.config import get_settings
from app.data.providers.base import ProviderError
from app.data.providers.mock import MockProvider
from app.data.providers.yfinance_provider import YFINANCE_LOOKBACK_DAYS, YFinanceUsProvider
from app.data.repository import list_instruments, save_bars
from app.db.session import SessionLocal


def _provider(name: str):
    if name == "mock":
        return MockProvider()
    if name == "yfinance":
        return YFinanceUsProvider()
    raise SystemExit(f"Unsupported US provider: {name}")


async def _collect(symbols: list[str], days: int, provider_name: str) -> int:
    if days > YFINANCE_LOOKBACK_DAYS and provider_name == "yfinance":
        print(f"yfinance 5-minute history is about {YFINANCE_LOOKBACK_DAYS} days. Requested {days}; using {YFINANCE_LOOKBACK_DAYS}.")
        days = YFINANCE_LOOKBACK_DAYS
    provider = _provider(provider_name)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    failures = 0
    for symbol in symbols:
        try:
            bars = await provider.get_bars(symbol, "5m", start, end)
        except ProviderError as exc:
            failures += 1
            print(exc)
            continue
        with SessionLocal() as session:
            result = save_bars(session, bars)
        print(f"{symbol}: saved={result.saved} rejected={len(result.rejected)} gaps={len(result.quality.gaps)}")
    return failures


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Collect US 5-minute bars")
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--provider", choices=["yfinance", "mock"], default=None)
    args = parser.parse_args()
    provider_name = args.provider or settings.market_data_provider
    if provider_name == "csv":
        raise SystemExit("CSV data is imported with scripts/import_csv.py")
    with SessionLocal() as session:
        if args.symbols:
            symbols = [symbol.upper() for symbol in args.symbols]
        else:
            symbols = [row.symbol for row in list_instruments(session, market="US", active_only=True)]
    if not symbols:
        raise SystemExit("No symbols to collect. Seed instruments or pass --symbols.")
    failures = asyncio.run(_collect(symbols, args.days, provider_name))
    if failures == len(symbols):
        sys.exit(1)


if __name__ == "__main__":
    main()
