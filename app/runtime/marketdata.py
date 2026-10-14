from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.calendar import is_trading_day, session_bar_starts, zone_for
from app.core.config import get_settings
from app.data.bar import NormalizedBar
from app.data.quality import inspect_bars
from app.data.repository import get_instrument, load_bars
from app.models.runtime import MarketDataHealth, ProcessingCheckpoint
from app.models.trading import Account
from app.runtime.cycle import YFinanceSource, _accept_bars
from app.runtime.schedule import bar_ready, market_day


def collect_market_data(session: Session, now: datetime, *, source=None, symbols: list[tuple[str, str]] | None = None, attempts: int = 3) -> list[MarketDataHealth]:
    """Fetch closed US bars and store health. This path does not create orders."""
    current = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    feed = source or YFinanceSource()
    targets = symbols or _us_symbols(session)
    rows: list[MarketDataHealth] = []
    for market, symbol in targets:
        if market != "US":
            rows.append(_health(session, market, symbol, "disabled", current, None, 0, 0, "日本株のオンライン取得はありません。"))
            continue
        if not is_trading_day(market, market_day(market, current)):
            rows.append(_health(session, market, symbol, "closed", current, None, 0, 0, "休場日です。"))
            continue
        delay = get_settings().runtime_bar_delay_seconds
        due = [start for start in session_bar_starts(market, market_day(market, current)) if bar_ready(start, current, delay)]
        if not due:
            rows.append(_health(session, market, symbol, "waiting", current, None, 0, 0, "確定した足はまだありません。"))
            continue
        start = due[0]
        end = due[-1] + timedelta(minutes=5)
        error = None
        fetched = []
        for _attempt in range(attempts):
            try:
                fetched = feed.fetch(market, symbol, start, end)
                error = None
                break
            except Exception as exc:  # noqa: BLE001
                error = str(exc)
                fetched = []
        if error:
            previous = _failure_count(session, market, symbol)
            rows.append(_health(session, market, symbol, "failed", current, None, 0, previous + 1, error))
            continue
        usable = _accept_bars(session, fetched, current)
        latest = max((bar.timestamp for bar in usable), default=None)
        gaps = inspect_bars(usable).gaps
        lag = None if latest is None else int((current - (latest + timedelta(minutes=5))).total_seconds())
        rows.append(_health(session, market, symbol, "ok", current, latest, len(gaps), 0, None))
    session.flush()
    return rows


def backfill_bars(session: Session, account: Account, market: str, symbol: str, bars, now: datetime) -> int:
    """Store older bars and advance the checkpoint without creating orders."""
    usable = _accept_bars(session, list(bars), now)
    for bar in usable:
        exists = session.scalar(
            select(ProcessingCheckpoint.id).where(
                ProcessingCheckpoint.account_id == account.id,
                ProcessingCheckpoint.market == market,
                ProcessingCheckpoint.symbol == symbol,
                ProcessingCheckpoint.bar_timestamp == bar.timestamp,
            )
        )
        if exists is None:
            session.add(
                ProcessingCheckpoint(
                    account_id=account.id,
                    market=market,
                    symbol=symbol,
                    bar_timestamp=bar.timestamp,
                )
            )
    session.flush()
    return len(usable)


class StoredBarSource:
    """Bars already stored by collection. Trading does not call the provider again."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def fetch(self, market: str, symbol: str, start: datetime, end: datetime) -> list[NormalizedBar]:
        instrument = get_instrument(self.session, market, symbol)
        if instrument is None:
            return []
        rows = load_bars(self.session, instrument.id, "5m", start=start, end=end)
        return [
            NormalizedBar(
                market=market,
                symbol=symbol,
                timestamp=row.timestamp,
                timeframe=row.timeframe,
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.volume,
                provider=row.provider,
                is_adjusted=bool(row.is_adjusted),
            )
            for row in rows
        ]


def gap_rows(session: Session, market: str, symbol: str) -> list[dict]:
    instrument = get_instrument(session, market, symbol)
    if instrument is None:
        return []
    stored = load_bars(session, instrument.id, "5m")
    if len(stored) < 2:
        return []
    missing = []
    ordered = sorted({row.timestamp for row in stored})
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if previous.astimezone(zone_for(market)).date() != current.astimezone(zone_for(market)).date():
            continue
        cursor = previous + timedelta(minutes=5)
        while cursor < current:
            missing.append({"symbol": symbol, "timestamp": cursor})
            cursor += timedelta(minutes=5)
    return missing


def _us_symbols(session: Session) -> list[tuple[str, str]]:
    account = session.scalar(select(Account).where(Account.name == "paper"))
    if account is None:
        return [("US", "AAPL")]
    markets = [str(item).upper() for item in account.markets or ["US"]]
    if "US" not in markets:
        return []
    return [("US", str(symbol).upper()) for symbol in (account.symbols or ["AAPL"])]


def _health(session, market, symbol, status, fetched_at, latest, gaps, failures, message) -> MarketDataHealth:
    row = MarketDataHealth(
        market=market,
        symbol=symbol,
        provider="yfinance" if market == "US" else "unavailable",
        status=status,
        fetched_at=fetched_at,
        latest_bar_at=latest,
        lag_seconds=None if latest is None else int((fetched_at - (latest + timedelta(minutes=5))).total_seconds()),
        gap_count=gaps,
        failure_count=failures,
        message=message,
    )
    session.add(row)
    return row


def _failure_count(session, market, symbol) -> int:
    value = session.scalar(
        select(func.max(MarketDataHealth.failure_count)).where(MarketDataHealth.market == market, MarketDataHealth.symbol == symbol)
    )
    return int(value or 0)
