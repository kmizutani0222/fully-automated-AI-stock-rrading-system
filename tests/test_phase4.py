from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.calendar import session_bar_starts
from app.core.config import Settings, assert_phase1_safety, get_settings
from app.data.bar import NormalizedBar
from app.data.normalizer import is_bar_closed
from app.data.providers.base import ProviderError
from app.db.session import SessionLocal
from app.models.trading import CashLedgerEntry, ExecutionRecord, OrderRecord
from app.runtime.accounts import cash_balances, create_account
from app.runtime.cycle import CYCLE_LOCK, StaticSource, run_cycle
from app.runtime.schedule import bar_ready
from app.models.runtime import ProcessingCheckpoint, SchedulerJob

UTC = timezone.utc
BAR1 = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
BAR2 = BAR1 + timedelta(minutes=5)
CLOSED = timedelta(minutes=5, seconds=5)


@pytest.fixture
def db(database_ready):
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def test_five_minute_bar_is_closed_only_after_its_window():
    assert is_bar_closed(BAR1, BAR1 + timedelta(minutes=4, seconds=59)) is False
    assert bar_ready(BAR1, BAR1 + timedelta(minutes=5) - timedelta(seconds=1), 0) is False
    assert is_bar_closed(BAR1, BAR1 + CLOSED) is True
    assert bar_ready(BAR1, BAR1 + CLOSED, 0) is True


def test_arrival_delay_holds_a_closed_bar(db: Session):
    account = _account(db)
    _checkpoint(db, account, BAR1 - timedelta(minutes=5))
    source = StaticSource([_bar(BAR1, "100")])
    early = run_cycle(db, BAR1 + timedelta(minutes=5, seconds=10), account_name=account.name, source=source, delay_seconds=30, job_name=account.name)
    assert early.status == "SKIPPED"
    assert _orders(db, account.id) == []
    ready = run_cycle(db, BAR1 + timedelta(minutes=5, seconds=30), account_name=account.name, source=source, delay_seconds=30, job_name=account.name)
    assert ready.status == "SUCCEEDED", ready.reason
    assert len(_orders(db, account.id)) == 1


def test_overlapping_cycle_is_rejected():
    assert CYCLE_LOCK.acquire(blocking=False)
    try:
        session = SessionLocal()
        try:
            result = run_cycle(session, datetime.now(UTC), account_name="missing")
        finally:
            session.close()
        assert result.status == "SKIPPED"
        assert result.reason == "already_running"
    finally:
        CYCLE_LOCK.release()


def test_same_bar_is_not_processed_twice(db: Session):
    account = _account(db)
    _checkpoint(db, account, BAR1 - timedelta(minutes=5))
    source = StaticSource([_bar(BAR1, "100")])
    now = BAR1 + CLOSED
    first = run_cycle(db, now, account_name=account.name, source=source, delay_seconds=0, job_name=account.name)
    second = run_cycle(db, now, account_name=account.name, source=source, delay_seconds=0, job_name=account.name)
    assert first.status == "SUCCEEDED", first.reason
    assert second.status == "SKIPPED"
    assert len(_orders(db, account.id)) == 1


def test_worker_restart_takes_a_stale_lock_and_the_unprocessed_bar(db: Session):
    account = _account(db)
    _checkpoint(db, account, BAR1 - timedelta(minutes=5))
    source = StaticSource([_bar(BAR1, "100", close="100"), _bar(BAR2, "110")])
    opened = run_cycle(db, BAR1 + CLOSED, account_name=account.name, source=source, delay_seconds=0, job_name=account.name)
    assert opened.orders == 1
    job = db.scalar(select(SchedulerJob).where(SchedulerJob.name == account.name))
    job.status = "RUNNING"
    job.heartbeat_at = datetime.now(UTC) - timedelta(minutes=10)
    db.commit()
    resumed = run_cycle(db, BAR2 + CLOSED, account_name=account.name, source=source, delay_seconds=0, job_name=account.name)
    assert resumed.status == "SUCCEEDED", resumed.reason
    order = _orders(db, account.id)[0]
    assert order.status == "FILLED"
    assert job.status == "IDLE"


def test_resting_order_is_restored_and_fills_on_the_next_bar(db: Session):
    account = _account(db)
    _checkpoint(db, account, BAR1 - timedelta(minutes=5))
    source = StaticSource([_bar(BAR1, "100", close="100"), _bar(BAR2, "110")])
    run_cycle(db, BAR1 + CLOSED, account_name=account.name, source=source, delay_seconds=0, job_name=account.name)
    resting = _orders(db, account.id)[0]
    assert resting.status == "SUBMITTED"
    assert resting.execute_at == BAR2
    run_cycle(db, BAR2 + CLOSED, account_name=account.name, source=source, delay_seconds=0, job_name=account.name)
    db.expire_all()
    filled = db.get(OrderRecord, resting.id)
    execution = db.scalar(select(ExecutionRecord).where(ExecutionRecord.order_id == filled.id))
    assert filled.status == "FILLED"
    assert execution.executed_at > filled.created_at
    assert execution.price == Decimal("110.00000000") or execution.price == Decimal("110")
    assert execution.price > Decimal("100")


def test_virtual_balance_drops_by_the_fill_cost(db: Session):
    account = _account(db, jpy=Decimal("1000000"), usd=Decimal("10000"))
    _checkpoint(db, account, BAR1 - timedelta(minutes=5))
    source = StaticSource([_bar(BAR1, "100"), _bar(BAR2, "100")])
    run_cycle(db, BAR1 + CLOSED, account_name=account.name, source=source, delay_seconds=0, job_name=account.name)
    run_cycle(db, BAR2 + CLOSED, account_name=account.name, source=source, delay_seconds=0, job_name=account.name)
    entry = db.scalar(select(CashLedgerEntry).where(CashLedgerEntry.account_id == account.id, CashLedgerEntry.reason == "BUY"))
    assert entry is not None
    assert entry.amount < 0
    assert entry.balance_after == cash_balances(db, account.id)["USD"]
    assert entry.currency == "USD"


def test_cash_stays_inside_its_currency(db: Session):
    account = _account(db, jpy=Decimal("1000000"), usd=Decimal("10000"))
    _checkpoint(db, account, BAR1 - timedelta(minutes=5))
    before = cash_balances(db, account.id)
    source = StaticSource([_bar(BAR1, "100"), _bar(BAR2, "100")])
    run_cycle(db, BAR1 + CLOSED, account_name=account.name, source=source, delay_seconds=0, job_name=account.name)
    run_cycle(db, BAR2 + CLOSED, account_name=account.name, source=source, delay_seconds=0, job_name=account.name)
    after = cash_balances(db, account.id)
    assert after["JPY"] == before["JPY"] == Decimal("1000000")
    assert after["USD"] < before["USD"]
    assert after["USD"] > 0


def test_untrained_ai_mode_places_no_order(db: Session):
    account = _account(db, mode="AI")
    _checkpoint(db, account, BAR1 - timedelta(minutes=5))
    result = run_cycle(
        db,
        BAR1 + CLOSED,
        account_name=account.name,
        source=StaticSource([_bar(BAR1, "100")]),
        delay_seconds=0,
        job_name=account.name,
    )
    assert result.status == "SUCCEEDED", result.reason
    assert result.orders == 0
    assert _orders(db, account.id) == []


def test_rule_mode_submits_on_the_signal_bar_and_fills_later(db: Session):
    account = _account(db, mode="RULE_BASED")
    _checkpoint(db, account, BAR1 - timedelta(minutes=5))
    source = StaticSource([_bar(BAR1, "100", close="90"), _bar(BAR2, "110")])
    first = run_cycle(db, BAR1 + CLOSED, account_name=account.name, source=source, delay_seconds=0, job_name=account.name)
    assert first.orders == 1
    assert _orders(db, account.id)[0].status == "SUBMITTED"
    run_cycle(db, BAR2 + CLOSED, account_name=account.name, source=source, delay_seconds=0, job_name=account.name)
    execution = db.scalar(select(ExecutionRecord).join(OrderRecord).where(OrderRecord.account_id == account.id))
    assert execution.executed_at == BAR2
    assert execution.price > Decimal("100")


def test_hybrid_uses_rules_only_when_fallback_is_enabled(db: Session):
    blocked = _account(db, mode="HYBRID", fallback=False)
    allowed = _account(db, mode="HYBRID", fallback=True)
    for account in (blocked, allowed):
        _checkpoint(db, account, BAR1 - timedelta(minutes=5))
        run_cycle(
            db,
            BAR1 + CLOSED,
            account_name=account.name,
            source=StaticSource([_bar(BAR1, "100")]),
            delay_seconds=0,
            job_name=account.name,
        )
    assert _orders(db, blocked.id) == []
    assert len(_orders(db, allowed.id)) == 1


def test_paused_mode_blocks_new_orders(db: Session):
    account = _account(db, mode="PAUSED")
    _checkpoint(db, account, BAR1 - timedelta(minutes=5))
    result = run_cycle(
        db,
        BAR1 + CLOSED,
        account_name=account.name,
        source=StaticSource([_bar(BAR1, "100")]),
        delay_seconds=0,
        job_name=account.name,
    )
    assert result.orders == 0
    assert _orders(db, account.id) == []


def test_replaying_a_bar_does_not_duplicate_the_order(db: Session):
    account = _account(db)
    _checkpoint(db, account, BAR1 - timedelta(minutes=5))
    source = StaticSource([_bar(BAR1, "100")])
    run_cycle(db, BAR1 + CLOSED, account_name=account.name, source=source, delay_seconds=0, job_name=account.name)
    marker = db.scalar(
        select(ProcessingCheckpoint).where(ProcessingCheckpoint.account_id == account.id, ProcessingCheckpoint.bar_timestamp == BAR1)
    )
    db.delete(marker)
    db.commit()
    run_cycle(db, BAR1 + CLOSED, account_name=account.name, source=source, delay_seconds=0, job_name=account.name)
    assert len(_orders(db, account.id)) == 1


def test_database_failure_rolls_back_the_cycle(db: Session):
    account = _account(db)
    _checkpoint(db, account, BAR1 - timedelta(minutes=5))

    def broken():
        raise RuntimeError("database unavailable")

    db.commit = broken
    result = run_cycle(
        db,
        BAR1 + CLOSED,
        account_name=account.name,
        source=StaticSource([_bar(BAR1, "100")]),
        delay_seconds=0,
        job_name=account.name,
    )
    assert result.status == "FAILED"
    assert "database unavailable" in result.reason
    with SessionLocal() as check:
        count = check.scalar(select(func.count()).select_from(OrderRecord).where(OrderRecord.account_id == account.id))
        assert count == 0


def test_fetch_failure_does_not_invent_prices(db: Session):
    account = _account(db)
    _checkpoint(db, account, BAR1 - timedelta(minutes=5))

    class Down:
        def fetch(self, market, symbol, start, end):
            raise ProviderError("yfinance request failed")

    result = run_cycle(db, BAR1 + CLOSED, account_name=account.name, source=Down(), delay_seconds=0, job_name=account.name)
    assert result.status == "FAILED"
    assert "yfinance" in result.reason
    assert _orders(db, account.id) == []


def test_holiday_does_not_trade(db: Session):
    account = _account(db)
    result = run_cycle(
        db,
        datetime(2024, 1, 1, 15, 0, tzinfo=UTC),
        account_name=account.name,
        source=StaticSource([_bar(datetime(2024, 1, 1, 14, 30, tzinfo=UTC), "100")]),
        delay_seconds=0,
        job_name=account.name,
    )
    assert result.status == "SKIPPED"
    assert _orders(db, account.id) == []
    assert session_bar_starts("US", datetime(2024, 1, 1).date()) == []


def test_us_session_follows_daylight_saving_and_early_close():
    winter = session_bar_starts("US", datetime(2024, 1, 2).date())
    summer = session_bar_starts("US", datetime(2024, 3, 11).date())
    early = session_bar_starts("US", datetime(2024, 7, 3).date())
    ny = ZoneInfo("America/New_York")
    assert winter[0].astimezone(ny).hour == 9 and winter[0].astimezone(ny).minute == 30
    assert summer[0].astimezone(ny).hour == 9 and summer[0].astimezone(ny).minute == 30
    assert winter[0].hour == 14
    assert summer[0].hour == 13
    assert early[-1].astimezone(ny).hour == 12 and early[-1].astimezone(ny).minute == 55


def test_pause_endpoint_is_the_emergency_stop(database_ready):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    token = get_settings().admin_token
    created = client.post(
        "/api/v1/accounts",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": f"halt-{uuid4().hex[:8]}", "mode": "RULE_BASED", "usd_cash": "1000", "strategies": ["buy_hold"], "symbols": ["AAPL"]},
    )
    assert created.status_code == 201
    account_id = created.json()["id"]
    stopped = client.post("/api/v1/runtime/pause", headers={"Authorization": f"Bearer {token}"}, json={"account_id": account_id})
    assert stopped.status_code == 200
    assert stopped.json()["mode"] == "PAUSED"


def test_mutations_require_authentication(database_ready):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    assert client.post("/api/v1/runtime/start", json={"mode": "RULE_BASED"}).status_code == 401
    assert client.post("/api/v1/runtime/start", headers={"Authorization": "Bearer wrong"}, json={"mode": "RULE_BASED"}).status_code == 401
    status = client.get("/api/v1/runtime/status")
    assert status.status_code == 200
    assert status.json()["live_trading_enabled"] is False


def test_live_mode_stays_refused(database_ready):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    token = get_settings().admin_token
    refused = client.post("/api/v1/runtime/start", headers={"Authorization": f"Bearer {token}"}, json={"mode": "LIVE"})
    assert refused.status_code == 400
    with pytest.raises(RuntimeError, match="LIVE"):
        assert_phase1_safety(Settings(live_trading_enabled=True))
    with pytest.raises(RuntimeError, match="LIVE"):
        assert_phase1_safety(Settings(trading_mode="LIVE"))


def test_running_job_lock_rejects_a_second_worker(db: Session):
    account = _account(db)
    db.add(SchedulerJob(name=account.name, status="RUNNING", heartbeat_at=datetime.now(UTC)))
    db.commit()
    result = run_cycle(db, BAR1 + CLOSED, account_name=account.name, source=StaticSource([]), delay_seconds=0, job_name=account.name)
    assert result.status == "SKIPPED"
    assert result.reason == "already_running"


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


def _account(db: Session, *, mode: str = "RULE_BASED", fallback: bool = False, jpy: Decimal = Decimal("0"), usd: Decimal = Decimal("10000")):
    account = create_account(
        db,
        name=f"p4-{uuid4().hex[:10]}",
        base_currency="JPY",
        jpy_cash=jpy,
        usd_cash=usd,
        markets=["US"],
        strategies=["buy_hold"],
        symbols=["AAPL"],
        mode=mode,
        fallback_to_rules=fallback,
        risk_settings={"slippage_pct": "0", "spread_pct": "0"},
    )
    db.commit()
    return account


def _checkpoint(db: Session, account, timestamp: datetime) -> None:
    db.add(ProcessingCheckpoint(account_id=account.id, market="US", symbol="AAPL", bar_timestamp=timestamp))
    db.commit()


def _bar(timestamp: datetime, open_price: str, close: str | None = None) -> NormalizedBar:
    close_price = close or open_price
    low = min(Decimal(open_price), Decimal(close_price))
    high = max(Decimal(open_price), Decimal(close_price))
    return NormalizedBar(
        market="US",
        symbol="AAPL",
        timestamp=timestamp,
        timeframe="5m",
        open=Decimal(open_price),
        high=high,
        low=low,
        close=Decimal(close_price),
        volume=Decimal("1000000"),
        provider="runtime",
    )


def _orders(db: Session, account_id: int) -> list[OrderRecord]:
    db.expire_all()
    return list(db.scalars(select(OrderRecord).where(OrderRecord.account_id == account_id)).all())
