from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.data.repository import ensure_instrument
from app.db.session import SessionLocal
from app.models import MarketBar
from app.models.ai import StrategyDecision
from app.models.trading import OrderRecord
from app.runtime.accounts import cash_balances, create_account
from app.trading.config import TradingConfig
from app.trading.ema_diagnostic import diagnose_ema_series, ema_diagnostic
from app.trading.indicators import ema
from app.trading.strategies import EmaCrossStrategy, PriceBar

UTC = timezone.utc
START = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)


def test_ema_diagnostic_reports_insufficient_data():
    closes = [Decimal("100")] * 10
    rows = diagnose_ema_series(closes, [_at(index) for index in range(len(closes))])
    latest = rows[-1]
    assert latest["sufficient"] is False
    assert latest["reason"] == "insufficient_data"
    assert latest["bar_count"] == 10
    assert latest["minimum_bars"] == 22
    assert latest["ema_slow"] is None
    assert "データ不足" in latest["reason_text"]
    assert latest["signal"] is None


def test_ema_diagnostic_reports_no_cross_and_the_change_from_the_previous_bar():
    closes = [Decimal("100")] * 30
    rows = diagnose_ema_series(closes, [_at(index) for index in range(len(closes))])
    latest = rows[-1]
    assert latest["sufficient"] is True
    assert latest["bar_count"] == 30
    assert Decimal(latest["ema_fast"]) == Decimal("100")
    assert Decimal(latest["ema_slow"]) == Decimal("100")
    assert Decimal(latest["previous_ema_fast"]) == Decimal("100")
    assert Decimal(latest["previous_ema_slow"]) == Decimal("100")
    assert Decimal(latest["ema_fast_change"]) == Decimal("0")
    assert Decimal(latest["ema_slow_change"]) == Decimal("0")
    assert latest["cross"] == "none"
    assert latest["signal"] is None
    assert latest["reason"] == "no_cross"
    assert "クロスなし" in latest["reason_text"]


def test_ema_diagnostic_matches_the_strategy_cross_without_changing_books(db: Session):
    flat = [Decimal("100")] * 22
    closes = flat + [Decimal("200")]
    timestamps = [_at(index) for index in range(len(closes))]
    rows = diagnose_ema_series(closes, timestamps)
    latest = rows[-1]
    fast = ema(closes, 9)
    slow = ema(closes, 21)
    assert Decimal(latest["ema_fast"]) == fast[-1]
    assert Decimal(latest["ema_slow"]) == slow[-1]
    assert Decimal(latest["ema_fast_change"]) == fast[-1] - fast[-2]
    assert Decimal(latest["ema_slow_change"]) == slow[-1] - slow[-2]
    assert latest["cross"] == "up"
    assert latest["signal"] == "BUY"
    bars = [PriceBar("AAPL", timestamp, close, close, close, close) for timestamp, close in zip(timestamps, closes, strict=True)]
    signal = EmaCrossStrategy(TradingConfig()).evaluate(bars)
    assert signal is not None and signal.side == "BUY"
    symbol = f"E{uuid4().hex[:8]}".upper()
    account = create_account(
        db,
        name=f"ema-{uuid4().hex[:8]}",
        base_currency="JPY",
        jpy_cash=Decimal("0"),
        usd_cash=Decimal("10000"),
        markets=["US"],
        strategies=["ema_cross"],
        symbols=[symbol],
        mode="RULE_BASED",
        fallback_to_rules=False,
        risk_settings={},
    )
    instrument = ensure_instrument(db, "US", symbol)
    for timestamp, close in zip(timestamps, closes, strict=True):
        db.add(
            MarketBar(
                instrument_id=instrument.id,
                timeframe="5m",
                timestamp=timestamp,
                open=close,
                high=close,
                low=close,
                close=close,
                volume=Decimal("100"),
                provider="diagnostic",
            )
        )
    db.commit()
    before_orders = db.scalar(select(func.count()).select_from(OrderRecord).where(OrderRecord.account_id == account.id))
    before_decisions = db.scalar(select(func.count()).select_from(StrategyDecision))
    before_cash = cash_balances(db, account.id)["USD"]
    client = TestClient(__import__("app.main", fromlist=["app"]).app)
    response = client.get("/api/v1/paper/ema-diagnostic", params={"symbol": symbol, "limit": 5})
    assert response.status_code == 200
    body = response.json()
    assert body["read_only"] is True
    assert body["bar_count"] == len(closes)
    assert body["latest"]["cross"] == "up"
    assert body["latest"]["bar_count"] == len(closes)
    assert len(body["history"]) == 5
    assert body["history"][0]["timestamp"].startswith("2024-01-02")
    assert "データ不足" in body["history"][-1]["reason_text"]
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(OrderRecord).where(OrderRecord.account_id == account.id)) == before_orders
    assert db.scalar(select(func.count()).select_from(StrategyDecision)) == before_decisions
    assert cash_balances(db, account.id)["USD"] == before_cash
    stored = ema_diagnostic(db, symbol=symbol, now=datetime(2024, 1, 3, tzinfo=UTC))
    assert stored["latest"]["reason"] == "cross_up"


def test_ema_history_keeps_an_explicit_insufficient_row_for_each_early_bar():
    closes = [Decimal("100")] * 22
    rows = diagnose_ema_series(closes, [_at(index) for index in range(len(closes))])
    assert len(rows) == 22
    assert all(row["reason"] == "insufficient_data" for row in rows[:-1])
    assert rows[-1]["sufficient"] is True
    assert rows[-1]["cross"] == "none"


def _at(index: int) -> datetime:
    return START + timedelta(minutes=5 * index)


@pytest.fixture
def db(database_ready):
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture(scope="session")
def database_ready():
    from sqlalchemy import text

    from app.db.session import engine

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL is not available: {exc}")
    return engine
