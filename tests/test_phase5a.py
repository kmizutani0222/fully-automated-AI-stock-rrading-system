from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.calendar import session_bar_starts
from app.core.config import Settings, assert_phase1_safety, get_settings
from app.data.bar import NormalizedBar
from app.data.providers.base import ProviderError
from app.db.session import SessionLocal
from app.models import MarketBar
from app.models.ai import StrategyDecision
from app.models.runtime import ProcessingCheckpoint
from app.models.trading import CashLedgerEntry, ExecutionRecord, OrderRecord
from app.runtime.accounts import cash_balances, create_account
from app.runtime.cycle import StaticSource, _accept_bars, run_cycle
from app.runtime.fills import paper_fill_quote
from app.runtime.marketdata import backfill_bars, collect_market_data, gap_rows
from app.runtime.performance import day_change, performance_report
from app.trading.indicators import ema

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


def test_order_does_not_fill_at_a_price_seen_before_submission():
    submitted = BAR2 + timedelta(seconds=5)
    quote = paper_fill_quote(
        side="BUY",
        submitted_at=submitted,
        bar_start=BAR1,
        open_price=Decimal("50"),
        close_price=Decimal("90"),
        received_at=submitted,
        slippage_pct=Decimal("0"),
        spread_pct=Decimal("0"),
    )
    assert quote is None
    later_open = paper_fill_quote(
        side="BUY",
        submitted_at=BAR2 + timedelta(minutes=5),
        bar_start=BAR2,
        open_price=Decimal("50"),
        close_price=Decimal("90"),
        received_at=BAR2 + timedelta(minutes=5, seconds=1),
    )
    assert later_open is None


def test_order_fills_at_a_price_observed_after_submission(db: Session):
    account = _account(db)
    _checkpoint(db, account, BAR1 - timedelta(minutes=5))
    source = StaticSource([_bar(BAR1, "100", "100"), _bar(BAR2, "90", "110")])
    opened = run_cycle(db, BAR1 + CLOSED, account_name=account.name, source=source, delay_seconds=0, job_name=account.name)
    assert opened.orders == 1
    resting = _orders(db, account.id)[0]
    assert resting.submitted_at == BAR1 + CLOSED
    filled = run_cycle(db, BAR2 + CLOSED, account_name=account.name, source=source, delay_seconds=0, job_name=account.name)
    assert filled.status == "SUCCEEDED", filled.reason
    execution = db.scalar(select(ExecutionRecord).where(ExecutionRecord.order_id == resting.id))
    assert execution.price == Decimal("110")
    assert execution.price != Decimal("90")
    assert execution.fill_observed_at > resting.submitted_at


def test_bid_ask_sets_the_fill():
    submitted = BAR1
    received = BAR2
    buy = paper_fill_quote(
        side="BUY",
        submitted_at=submitted,
        bar_start=BAR2,
        open_price=Decimal("100"),
        close_price=Decimal("100"),
        received_at=received,
        bid=Decimal("99"),
        ask=Decimal("101"),
    )
    sell = paper_fill_quote(
        side="SELL",
        submitted_at=submitted,
        bar_start=BAR2,
        open_price=Decimal("100"),
        close_price=Decimal("100"),
        received_at=received,
        bid=Decimal("99"),
        ask=Decimal("101"),
    )
    assert buy is not None and buy.price == Decimal("101") and buy.source == "ask"
    assert sell is not None and sell.price == Decimal("99") and sell.source == "bid"


def test_missing_bid_ask_uses_a_conservative_close():
    quote = paper_fill_quote(
        side="BUY",
        submitted_at=BAR1,
        bar_start=BAR2,
        open_price=Decimal("100"),
        close_price=Decimal("110"),
        received_at=BAR2 + CLOSED,
        spread_pct=Decimal("0.0002"),
        slippage_pct=Decimal("0.0005"),
    )
    assert quote is not None
    assert quote.price == Decimal("110") * (Decimal(1) + Decimal("0.0005") + Decimal("0.0001"))
    assert quote.source == "close"


def test_failed_price_fetch_leaves_the_order_open(db: Session):
    account = _account(db)
    _checkpoint(db, account, BAR1 - timedelta(minutes=5))
    source = StaticSource([_bar(BAR1, "100")])
    opened = run_cycle(db, BAR1 + CLOSED, account_name=account.name, source=source, delay_seconds=0, job_name=account.name)
    assert opened.orders == 1

    class Broken:
        def fetch(self, market, symbol, start, end):
            raise ProviderError("yahoo unavailable")

    failed = run_cycle(db, BAR2 + CLOSED, account_name=account.name, source=Broken(), delay_seconds=0, job_name=account.name)
    assert failed.status == "FAILED"
    order = _orders(db, account.id)[0]
    assert order.status == "SUBMITTED"
    assert db.scalar(select(func.count()).select_from(ExecutionRecord).where(ExecutionRecord.order_id == order.id)) == 0


def test_usd_virtual_deposit(database_ready):
    account_id, balances = _deposit_once()
    assert balances["USD"] == Decimal("10000") or balances["USD"] == Decimal("10000.00000000")
    assert account_id > 0


def test_deposit_is_not_applied_twice(database_ready):
    account_id, balances = _deposit_twice()
    session = SessionLocal()
    try:
        rows = session.scalars(select(CashLedgerEntry).where(CashLedgerEntry.idempotency_key == f"deposit-{account_id}")).all()
        assert len(rows) == 1
        assert balances["USD"] == Decimal("10000") or balances["USD"] == Decimal("10000.00000000")
    finally:
        session.close()


def test_jpy_balance_is_unchanged_by_a_usd_deposit(database_ready):
    _account_id, balances = _deposit_once()
    assert balances["JPY"] == Decimal("1000000") or balances["JPY"] == Decimal("1000000.00000000")


def test_market_data_failure_is_recorded_without_a_synthetic_bar(db: Session):
    symbol = f"P5F{uuid4().hex[:4]}".upper()

    class Broken:
        def fetch(self, market, symbol, start, end):
            raise ProviderError("timeout")

    before = db.scalar(select(func.count()).select_from(MarketBar).where(MarketBar.provider == "synthetic")) or 0
    rows = collect_market_data(db, BAR1 + timedelta(minutes=5, seconds=40), source=Broken(), symbols=[("US", symbol)])
    db.commit()
    assert rows[0].status == "failed"
    assert rows[0].failure_count >= 1
    assert before == (db.scalar(select(func.count()).select_from(MarketBar).where(MarketBar.provider == "synthetic")) or 0)


def test_missing_bar_is_reported(db: Session):
    symbol = f"P5G{uuid4().hex[:4]}".upper()
    now = BAR1 + timedelta(minutes=20)
    _accept_bars(db, [_bar(BAR1, "10", symbol=symbol), _bar(BAR1 + timedelta(minutes=10), "11", symbol=symbol)], now)
    db.commit()
    missing = gap_rows(db, "US", symbol)
    assert [row["timestamp"] for row in missing] == [BAR1 + timedelta(minutes=5)]


def test_duplicate_bar_is_stored_once(db: Session):
    symbol = f"P5D{uuid4().hex[:4]}".upper()
    bar = _bar(BAR1, "10", symbol=symbol)
    _accept_bars(db, [bar], BAR1 + CLOSED)
    _accept_bars(db, [bar], BAR1 + CLOSED)
    db.commit()
    from app.data.repository import get_instrument

    instrument = get_instrument(db, "US", symbol)
    count = db.scalar(select(func.count()).select_from(MarketBar).where(MarketBar.instrument_id == instrument.id, MarketBar.timestamp == BAR1))
    assert count == 1


def test_backfill_does_not_create_orders(db: Session):
    account = _account(db, strategies=["ema_cross"])
    bars = [_bar(BAR1, "10", symbol="AAPL"), _bar(BAR2, "11", symbol="AAPL")]
    stored = backfill_bars(db, account, "US", "AAPL", bars, BAR2 + CLOSED)
    db.commit()
    assert stored == 2
    assert _orders(db, account.id) == []
    marks = db.scalars(select(ProcessingCheckpoint).where(ProcessingCheckpoint.account_id == account.id)).all()
    assert len(marks) == 2


def test_ema_cross_runs_on_the_latest_closed_bar(db: Session):
    symbol = f"P5E{uuid4().hex[:4]}".upper()
    closes = [Decimal(10)] * 6 + [Decimal(11), Decimal(12), Decimal(13)]
    fast = ema(closes, 3)
    slow = ema(closes, 5)
    expected = next(
        index
        for index in range(1, len(closes))
        if None not in {fast[index], slow[index], fast[index - 1], slow[index - 1]}
        and fast[index - 1] <= slow[index - 1]
        and fast[index] > slow[index]
    )
    starts = session_bar_starts("US", date(2024, 1, 2))
    bars = [_bar(starts[index], str(close), symbol=symbol) for index, close in enumerate(closes)]
    account = _account(db, strategies=["ema_cross"], symbols=[symbol], risk={"ema_fast": 3, "ema_slow": 5, "slippage_pct": "0", "spread_pct": "0"})
    backfill_bars(db, account, "US", symbol, bars[:expected], starts[expected] + CLOSED)
    db.commit()
    result = run_cycle(
        db,
        starts[expected] + CLOSED,
        account_name=account.name,
        source=StaticSource([bars[expected]]),
        delay_seconds=0,
        job_name=account.name,
    )
    assert result.status == "SUCCEEDED", result.reason
    assert result.orders == 1
    assert _orders(db, account.id)[0].strategy_name == "ema_cross"


def test_no_signal_creates_no_order(db: Session):
    symbol = f"P5N{uuid4().hex[:4]}".upper()
    account = _account(db, strategies=["ema_cross"], symbols=[symbol])
    _checkpoint(db, account, BAR1 - timedelta(minutes=5))
    result = run_cycle(db, BAR1 + CLOSED, account_name=account.name, source=StaticSource([_bar(BAR1, "100", symbol=symbol)]), delay_seconds=0, job_name=account.name)
    assert result.status == "SUCCEEDED", result.reason
    assert _orders(db, account.id) == []
    decision = db.scalar(select(StrategyDecision).where(StrategyDecision.reason == "no_signal").order_by(StrategyDecision.id.desc()))
    assert decision is not None
    assert decision.choice == "CASH"


def test_rejection_reason_is_stored(db: Session):
    symbol = f"P5R{uuid4().hex[:4]}".upper()
    account = _account(db, strategies=["buy_hold"], symbols=[symbol], risk={"stop_loss_pct": "0", "slippage_pct": "0", "spread_pct": "0"})
    _checkpoint(db, account, BAR1 - timedelta(minutes=5))
    result = run_cycle(db, BAR1 + CLOSED, account_name=account.name, source=StaticSource([_bar(BAR1, "100", symbol=symbol)]), delay_seconds=0, job_name=account.name)
    assert result.orders == 0
    decision = db.scalar(select(StrategyDecision).where(StrategyDecision.reason == "invalid_stop").order_by(StrategyDecision.id.desc()))
    assert decision is not None
    assert _orders(db, account.id) == []


def test_paused_account_does_not_open_a_new_buy(db: Session):
    symbol = f"P5P{uuid4().hex[:4]}".upper()
    account = _account(db, mode="PAUSED", strategies=["buy_hold"], symbols=[symbol])
    _checkpoint(db, account, BAR1 - timedelta(minutes=5))
    result = run_cycle(db, BAR1 + CLOSED, account_name=account.name, source=StaticSource([_bar(BAR1, "100", symbol=symbol)]), delay_seconds=0, job_name=account.name)
    assert result.orders == 0
    assert _orders(db, account.id) == []
    decision = db.scalar(select(StrategyDecision).where(StrategyDecision.reason == "paused").order_by(StrategyDecision.id.desc()))
    assert decision is not None


def test_restart_restores_the_open_order_and_fills_later(db: Session):
    account = _account(db)
    _checkpoint(db, account, BAR1 - timedelta(minutes=5))
    source = StaticSource([_bar(BAR1, "100", "100"), _bar(BAR2, "90", "110")])
    from app.models.runtime import SchedulerJob

    run_cycle(db, BAR1 + CLOSED, account_name=account.name, source=source, delay_seconds=0, job_name=account.name)
    job = db.scalar(select(SchedulerJob).where(SchedulerJob.name == account.name))
    job.status = "RUNNING"
    job.heartbeat_at = datetime.now(UTC) - timedelta(minutes=10)
    db.commit()
    resumed = run_cycle(db, BAR2 + CLOSED, account_name=account.name, source=source, delay_seconds=0, job_name=account.name)
    assert resumed.status == "SUCCEEDED", resumed.reason
    order = _orders(db, account.id)[0]
    execution = db.scalar(select(ExecutionRecord).where(ExecutionRecord.order_id == order.id))
    assert order.status == "FILLED"
    assert order.submitted_at is not None
    assert execution.price == Decimal("110")
    assert execution.filled_at is not None
    assert execution.filled_at > order.submitted_at


def test_replaying_a_bar_does_not_duplicate_the_order(db: Session):
    account = _account(db)
    _checkpoint(db, account, BAR1 - timedelta(minutes=5))
    source = StaticSource([_bar(BAR1, "100")])
    first = run_cycle(db, BAR1 + CLOSED, account_name=account.name, source=source, delay_seconds=0, job_name=account.name)
    assert first.orders == 1
    for row in db.scalars(select(ProcessingCheckpoint).where(ProcessingCheckpoint.account_id == account.id)).all():
        db.delete(row)
    db.commit()
    _checkpoint(db, account, BAR1 - timedelta(minutes=5))
    second = run_cycle(db, BAR1 + CLOSED, account_name=account.name, source=source, delay_seconds=0, job_name=account.name)
    assert second.orders == 0
    assert len(_orders(db, account.id)) == 1


def test_daily_pnl_is_null_until_two_equity_points_exist(db: Session):
    assert day_change(None, Decimal("10")) is None
    assert day_change(Decimal("100"), Decimal("90")) == Decimal("-10")
    account = _account(db, mode="PAUSED")
    from app.models.trading import PortfolioSnapshot

    db.add(PortfolioSnapshot(account_id=account.id, timestamp=BAR1, currency="USD", cash=Decimal("100"), market_value=Decimal("0"), equity=Decimal("100")))
    db.flush()
    report = performance_report(db, account.id)
    assert report["currencies"]["USD"]["day_pnl"] is None
    assert report["win_rate"] is None
    db.add(PortfolioSnapshot(account_id=account.id, timestamp=BAR2, currency="USD", cash=Decimal("90"), market_value=Decimal("0"), equity=Decimal("90")))
    db.flush()
    again = performance_report(db, account.id)
    assert Decimal(again["currencies"]["USD"]["day_pnl"]) == Decimal("-10")
    assert again["win_rate"] is None
    assert again["cost_total"] is None


def test_live_mode_is_still_refused(database_ready):
    client = TestClient(__import__("app.main", fromlist=["app"]).app)
    token = get_settings().admin_token
    refused = client.post("/api/v1/runtime/start", headers={"Authorization": f"Bearer {token}"}, json={"mode": "LIVE"})
    assert refused.status_code == 400
    with pytest.raises(RuntimeError, match="LIVE"):
        assert_phase1_safety(Settings(live_trading_enabled=True))
    with pytest.raises(RuntimeError, match="LIVE"):
        assert_phase1_safety(Settings(trading_mode="LIVE"))


def _deposit_once() -> tuple[int, dict]:
    return _deposit(times=1)


def _deposit_twice() -> tuple[int, dict]:
    return _deposit(times=2)


def _deposit(*, times: int) -> tuple[int, dict]:
    client = TestClient(__import__("app.main", fromlist=["app"]).app)
    token = get_settings().admin_token
    created = client.post(
        "/api/v1/accounts",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": f"usd-{uuid4().hex[:8]}", "mode": "PAUSED", "jpy_cash": "1000000", "usd_cash": "0", "strategies": ["ema_cross"], "symbols": ["AAPL"]},
    )
    assert created.status_code == 201
    account_id = created.json()["id"]
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": f"deposit-{account_id}"}
    body = {"account_id": account_id, "currency": "USD", "amount": "10000"}
    responses = [client.post("/api/v1/paper/deposits", headers=headers, json=body) for _ in range(times)]
    assert all(item.status_code == 200 for item in responses)
    assert len({item.json()["ledger_id"] for item in responses}) == 1
    session = SessionLocal()
    try:
        return account_id, cash_balances(session, account_id)
    finally:
        session.close()


def _account(db: Session, *, mode: str = "RULE_BASED", strategies: list[str] | None = None, symbols: list[str] | None = None, risk: dict | None = None):
    account = create_account(
        db,
        name=f"p5-{uuid4().hex[:10]}",
        base_currency="JPY",
        jpy_cash=Decimal("0"),
        usd_cash=Decimal("10000"),
        markets=["US"],
        strategies=strategies or ["buy_hold"],
        symbols=symbols or ["AAPL"],
        mode=mode,
        fallback_to_rules=False,
        risk_settings=risk or {"slippage_pct": "0", "spread_pct": "0"},
    )
    db.commit()
    return account


def _checkpoint(db: Session, account, timestamp: datetime) -> None:
    db.add(ProcessingCheckpoint(account_id=account.id, market="US", symbol=account.symbols[0], bar_timestamp=timestamp))
    db.commit()


def _bar(timestamp: datetime, open_price: str, close: str | None = None, symbol: str = "AAPL") -> NormalizedBar:
    close_price = close or open_price
    low = min(Decimal(open_price), Decimal(close_price))
    high = max(Decimal(open_price), Decimal(close_price))
    return NormalizedBar(
        market="US",
        symbol=symbol,
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
