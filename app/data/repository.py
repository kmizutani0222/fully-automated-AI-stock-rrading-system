from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.data.bar import NormalizedBar, RejectedBar
from app.data.normalizer import default_currency, require_market
from app.data.quality import QualityReport, drop_open_bars, inspect_bars, partition_bars
from app.models import Instrument, MarketBar


@dataclass
class SaveResult:
    saved: int
    rejected: list[RejectedBar]
    quality: QualityReport


def list_instruments(session: Session, market: str | None = None, active_only: bool = False) -> list[Instrument]:
    query = select(Instrument).order_by(Instrument.market, Instrument.symbol)
    if market:
        query = query.where(Instrument.market == market.upper())
    if active_only:
        query = query.where(Instrument.active.is_(True))
    return list(session.scalars(query).all())


def get_instrument(session: Session, market: str, symbol: str) -> Instrument | None:
    return session.scalar(
        select(Instrument).where(Instrument.market == market.upper(), Instrument.symbol == symbol.upper())
    )


def ensure_instrument(
    session: Session,
    market: str,
    symbol: str,
    *,
    name: str | None = None,
    exchange: str | None = None,
    currency: str | None = None,
) -> Instrument:
    market = require_market(market)
    symbol = symbol.strip().upper()
    existing = get_instrument(session, market, symbol)
    if existing is not None:
        return existing
    instrument = Instrument(
        market=market,
        symbol=symbol,
        name=name or symbol,
        exchange=exchange,
        currency=currency or default_currency(market),
        active=True,
    )
    session.add(instrument)
    session.flush()
    return instrument


def load_bars(
    session: Session,
    instrument_id: int,
    timeframe: str,
    *,
    provider: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[MarketBar]:
    query = select(MarketBar).where(MarketBar.instrument_id == instrument_id, MarketBar.timeframe == timeframe)
    if provider:
        query = query.where(MarketBar.provider == provider)
    if start:
        query = query.where(MarketBar.timestamp >= start)
    if end:
        query = query.where(MarketBar.timestamp < end)
    query = query.order_by(MarketBar.timestamp)
    return list(session.scalars(query).all())


def save_bars(session: Session, bars: list[NormalizedBar], *, now: datetime | None = None) -> SaveResult:
    quality = inspect_bars(bars)
    valid, _rejected = partition_bars(bars)
    closed, dropped = drop_open_bars(valid, now)
    quality.dropped_open_bars = dropped
    deduped: dict[tuple, NormalizedBar] = {}
    for bar in closed:
        deduped[(bar.market, bar.symbol, bar.timeframe, bar.timestamp, bar.provider)] = bar
    saved = 0
    for bar in deduped.values():
        instrument = ensure_instrument(session, bar.market, bar.symbol)
        statement = insert(MarketBar).values(
            instrument_id=instrument.id,
            timeframe=bar.timeframe,
            timestamp=bar.timestamp,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            vwap=bar.vwap,
            trade_count=bar.trade_count,
            provider=bar.provider,
            is_adjusted=bar.is_adjusted,
        )
        statement = statement.on_conflict_do_update(
            constraint="uq_market_bars_instrument_time_provider",
            set_={
                "open": statement.excluded.open,
                "high": statement.excluded.high,
                "low": statement.excluded.low,
                "close": statement.excluded.close,
                "volume": statement.excluded.volume,
                "vwap": statement.excluded.vwap,
                "trade_count": statement.excluded.trade_count,
                "is_adjusted": statement.excluded.is_adjusted,
            },
        )
        session.execute(statement)
        saved += 1
    session.commit()
    return SaveResult(saved=saved, rejected=quality.rejected, quality=quality)
