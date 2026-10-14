from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.data.bar import NormalizedBar
from app.db.session import SessionLocal
from app.models.ai import StrategyDecision
from app.models.runtime import AccountModeChange, ProcessingCheckpoint, SchedulerJob, TradingSession
from app.models.trading import Account, CashLedgerEntry, ExecutionRecord, OrderRecord, PositionRecord
from app.runtime.accounts import cash_balances, create_account, latest_session
from app.runtime.cycle import StaticSource, run_cycle
from app.runtime.daily_report import paper_daily_report
from app.runtime.fills import paper_fill_quote

UTC = timezone.utc
BAR = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
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


def test_verification_buy_fills_only_after_submission(db: Session):
    account, symbol, bars = _book(
        db,
        prices=["100", "110"],
        opens=["100", "109.50"],
        lows=["100", "109.50"],
        highs=["100", "110"],
    )
    _step(db, account, bars, BAR)
    order = _orders(db, account.id)[0]
    assert order.side == "BUY"
    assert order.status == "SUBMITTED"
    assert db.scalar(select(ExecutionRecord.id).where(ExecutionRecord.order_id == order.id)) is None
    _step(db, account, bars, _at(1))
    execution = db.scalar(select(ExecutionRecord).where(ExecutionRecord.order_id == order.id))
    assert execution.price == Decimal("110")
    assert execution.price != Decimal("109.50")
    assert execution.fill_observed_at > order.submitted_at
    assert order.status == "FILLED"
    _assert_books(db, account)


def test_verification_sell_order_closes_the_position(db: Session):
    account, symbol, bars = _book(db, prices=["100", "110", "120", "130", "140"])
    _step(db, account, bars, BAR)
    _step(db, account, bars, _at(1))
    account.strategies = ["rsi_reversion"]
    account.risk_settings = {
        "slippage_pct": "0",
        "spread_pct": "0",
        "stop_loss_pct": "0.5",
        "rsi_period": 2,
        "rsi_exit": "70",
    }
    db.commit()
    _step(db, account, bars, _at(2))
    _step(db, account, bars, _at(3))
    sell = next(row for row in _orders(db, account.id) if row.side == "SELL")
    assert sell.decision_reason == "rsi_at_or_above_70"
    assert sell.status == "SUBMITTED"
    assert db.scalar(select(ExecutionRecord.id).where(ExecutionRecord.order_id == sell.id)) is None
    _step(db, account, bars, _at(4))
    execution = db.scalar(select(ExecutionRecord).where(ExecutionRecord.order_id == sell.id))
    assert sell.status == "FILLED"
    assert execution.fill_observed_at > sell.submitted_at
    closed = db.scalar(select(PositionRecord).where(PositionRecord.account_id == account.id, PositionRecord.status == "CLOSED"))
    assert closed is not None
    assert closed.quantity == 0
    _assert_books(db, account)


def test_verification_partial_fill_matches_position_and_cash(db: Session):
    account, symbol, bars = _book(
        db,
        risk={"slippage_pct": "0", "spread_pct": "0", "participation_rate": "0.5"},
        prices=["100", "100"],
        volume="10",
    )
    _step(db, account, bars, BAR)
    _step(db, account, bars, _at(1))
    order = _orders(db, account.id)[0]
    assert order.status == "PARTIALLY_FILLED"
    assert order.filled_quantity == Decimal("5")
    assert order.filled_quantity < order.quantity
    position = db.scalar(select(PositionRecord).where(PositionRecord.account_id == account.id, PositionRecord.status == "OPEN"))
    assert position.quantity == order.filled_quantity
    assert cash_balances(db, account.id)["USD"] == Decimal("9500")
    _assert_books(db, account)


def test_verification_cancel_leaves_cash_and_creates_no_fill(db: Session):
    account, symbol, bars = _book(db, prices=["100", "110"])
    _step(db, account, bars, BAR)
    order = _orders(db, account.id)[0]
    client = TestClient(__import__("app.main", fromlist=["app"]).app)
    canceled = client.post(
        f"/api/v1/accounts/{account.id}/orders/{order.id}/cancel",
        headers={"Authorization": f"Bearer {get_settings().admin_token}"},
    )
    assert canceled.status_code == 200
    assert canceled.json()["status"] == "CANCELED"
    _step(db, account, bars, _at(1))
    db.expire_all()
    assert db.get(OrderRecord, order.id).status == "CANCELED"
    assert db.scalar(select(ExecutionRecord.id).where(ExecutionRecord.order_id == order.id)) is None
    assert cash_balances(db, account.id)["USD"] == Decimal("10000")


def test_verification_stop_loss_fills_on_a_later_bar(db: Session):
    account, symbol, bars = _book(
        db,
        risk={"slippage_pct": "0", "spread_pct": "0", "stop_loss_pct": "0.05"},
        prices=["100", "100", "96", "90"],
        lows=["100", "100", "90", "80"],
        opens=["100", "100", "96", "80"],
    )
    _step(db, account, bars, BAR)
    _step(db, account, bars, _at(1))
    held = db.scalar(select(PositionRecord).where(PositionRecord.account_id == account.id, PositionRecord.status == "OPEN"))
    assert held.stop_price == Decimal("95")
    _step(db, account, bars, _at(2))
    sell = next(row for row in _orders(db, account.id) if row.side == "SELL")
    assert sell.decision_reason == "stop_loss"
    assert sell.status == "SUBMITTED"
    assert db.scalar(select(ExecutionRecord.id).where(ExecutionRecord.order_id == sell.id)) is None
    _step(db, account, bars, _at(3))
    execution = db.scalar(select(ExecutionRecord).where(ExecutionRecord.order_id == sell.id))
    assert sell.status == "FILLED"
    assert execution.price == Decimal("90")
    assert execution.price != Decimal("80")
    assert execution.fill_observed_at > sell.submitted_at
    _assert_books(db, account)


def test_verification_take_profit_fills_on_a_later_bar(db: Session):
    account, symbol, bars = _book(
        db,
        risk={"slippage_pct": "0", "spread_pct": "0", "stop_loss_pct": "0.5", "take_profit_pct": "0.02"},
        prices=["100", "100", "110", "110"],
        highs=["100", "100", "120", "110"],
        lows=["100", "100", "100", "100"],
        opens=["100", "100", "110", "100"],
    )
    _step(db, account, bars, BAR)
    _step(db, account, bars, _at(1))
    held = db.scalar(select(PositionRecord).where(PositionRecord.account_id == account.id, PositionRecord.status == "OPEN"))
    assert held.take_profit == Decimal("102")
    _step(db, account, bars, _at(2))
    sell = next(row for row in _orders(db, account.id) if row.side == "SELL")
    assert sell.decision_reason == "take_profit"
    assert db.scalar(select(ExecutionRecord.id).where(ExecutionRecord.order_id == sell.id)) is None
    _step(db, account, bars, _at(3))
    execution = db.scalar(select(ExecutionRecord).where(ExecutionRecord.order_id == sell.id))
    assert execution.price == Decimal("110")
    assert execution.price != Decimal("100")
    assert execution.fill_observed_at > sell.submitted_at
    _assert_books(db, account)


def test_verification_stop_wins_when_stop_and_target_hit_together(db: Session):
    account, symbol, bars = _book(
        db,
        risk={"slippage_pct": "0", "spread_pct": "0", "stop_loss_pct": "0.05", "take_profit_pct": "0.01"},
        prices=["100", "100", "100"],
        highs=["100", "100", "120"],
        lows=["100", "100", "90"],
    )
    _step(db, account, bars, BAR)
    _step(db, account, bars, _at(1))
    _step(db, account, bars, _at(2))
    sell = next(row for row in _orders(db, account.id) if row.side == "SELL")
    assert sell.decision_reason == "stop_loss"
    assert sell.status == "SUBMITTED"
    assert db.scalar(select(ExecutionRecord.id).where(ExecutionRecord.order_id == sell.id)) is None


def test_verification_insufficient_cash_rejects_the_buy(db: Session):
    account, symbol, bars = _book(db, usd=Decimal("1"), risk={"slippage_pct": "0", "spread_pct": "0", "us_lot": "1"})
    result = _step(db, account, bars, BAR)
    assert result.orders == 0
    decision = db.scalar(select(StrategyDecision).where(StrategyDecision.reason == "INSUFFICIENT_CASH").order_by(StrategyDecision.id.desc()))
    assert decision is not None
    assert _orders(db, account.id) == []
    assert cash_balances(db, account.id)["USD"] == Decimal("1")


def test_verification_risk_limit_rejects_the_buy(db: Session):
    account, symbol, bars = _book(db, risk={"slippage_pct": "0", "spread_pct": "0", "max_positions": 0})
    result = _step(db, account, bars, BAR)
    assert result.orders == 0
    decision = db.scalar(select(StrategyDecision).where(StrategyDecision.reason == "MAX_POSITIONS").order_by(StrategyDecision.id.desc()))
    assert decision is not None
    assert _orders(db, account.id) == []


def test_verification_duplicate_order_is_blocked(db: Session):
    account, symbol, bars = _book(db, prices=["100"])
    first = _step(db, account, bars, BAR)
    assert first.orders == 1
    for row in db.scalars(select(ProcessingCheckpoint).where(ProcessingCheckpoint.account_id == account.id)).all():
        db.delete(row)
    db.commit()
    _anchor(db, account, symbol)
    second = _step(db, account, bars, BAR)
    assert second.orders == 0
    assert len(_orders(db, account.id)) == 1


def test_verification_restart_restores_the_unfilled_order(db: Session):
    account, symbol, bars = _book(db)
    _step(db, account, bars, BAR)
    job = db.scalar(select(SchedulerJob).where(SchedulerJob.name == account.name))
    job.status = "RUNNING"
    job.heartbeat_at = datetime.now(UTC) - timedelta(minutes=10)
    db.commit()
    resumed = _step(db, account, bars, _at(1))
    assert resumed.status == "SUCCEEDED"
    order = _orders(db, account.id)[0]
    execution = db.scalar(select(ExecutionRecord).where(ExecutionRecord.order_id == order.id))
    assert order.status == "FILLED"
    assert execution.fill_observed_at > order.submitted_at
    assert execution.price == Decimal("110")


def test_verification_does_not_fill_at_a_price_seen_before_submission():
    submitted = _at(1) + CLOSED
    quote = paper_fill_quote(
        side="BUY",
        submitted_at=submitted,
        bar_start=BAR,
        open_price=Decimal("90"),
        close_price=Decimal("110"),
        received_at=submitted,
        slippage_pct=Decimal("0"),
        spread_pct=Decimal("0"),
    )
    assert quote is None


def test_mode_history_survives_a_session_update(db: Session):
    account, _symbol, _bars = _book(db, prices=["100"])
    client = TestClient(__import__("app.main", fromlist=["app"]).app)
    token = {"Authorization": f"Bearer {get_settings().admin_token}"}
    paused = client.post("/api/v1/runtime/pause", headers={**token, "X-Request-Id": "req-pause"}, json={"account_id": account.id})
    resumed = client.post("/api/v1/runtime/resume", headers={**token, "X-Request-Id": "req-resume"}, json={"account_id": account.id})
    assert paused.status_code == 200
    assert resumed.status_code == 200
    db.expire_all()
    rows = db.scalars(select(AccountModeChange).where(AccountModeChange.account_id == account.id).order_by(AccountModeChange.id)).all()
    assert [(row.previous_mode, row.new_mode, row.request_id) for row in rows] == [
        ("RULE_BASED", "PAUSED", "req-pause"),
        ("PAUSED", "RULE_BASED", "req-resume"),
    ]
    session_row = latest_session(db, account.id)
    session_row.mode = "HYBRID"
    session_row.updated_at = datetime.now(UTC)
    db.commit()
    again = db.scalars(select(AccountModeChange).where(AccountModeChange.account_id == account.id).order_by(AccountModeChange.id)).all()
    assert [(row.previous_mode, row.new_mode, row.action) for row in again] == [(row.previous_mode, row.new_mode, row.action) for row in rows]
    stored = db.get(TradingSession, session_row.id)
    assert stored.mode == "HYBRID"
    status = client.get(f"/api/v1/paper/status?account_id={account.id}")
    assert status.status_code == 200
    body = status.json()
    assert body["account_role"] == "verification"
    assert body["account_name"] == account.name
    assert len(body["mode_changes"]) == 2


def test_daily_report_is_paper_only_and_does_not_evaluate_an_empty_day(db: Session):
    paper = _paper(db)
    empty = paper_daily_report(db, paper, date(1999, 1, 4))
    assert empty["account"] == "paper"
    assert empty["role"] == "operational"
    assert empty["bars_processed"] == 0
    assert empty["signals"] == 0
    assert empty["orders"] == 0
    assert empty["fills"] == 0
    assert empty["closes"] == 0
    assert empty["rejections"] == 0
    assert empty["realized_pnl"] is None
    assert empty["unrealized_pnl"] is None
    assert empty["cost_total"] is None
    assert empty["profitability_evaluated"] is False
    before = paper_daily_report(db, paper, date(2024, 1, 2))["orders"]
    account, _symbol, bars = _book(db, prices=["100"])
    _step(db, account, bars, BAR)
    after = paper_daily_report(db, paper, date(2024, 1, 2))["orders"]
    verify = paper_daily_report(db, account, date(2024, 1, 2))
    assert after == before
    assert verify["role"] == "verification"
    assert verify["orders"] == 1
    assert verify["profitability_evaluated"] is False
    client = TestClient(__import__("app.main", fromlist=["app"]).app)
    response = client.get("/api/v1/paper/daily-report", params={"day": "1999-01-04"})
    assert response.status_code == 200
    assert response.json()["account"] == "paper"
    assert response.json()["orders"] == 0
    assert response.json()["profitability_evaluated"] is False


def test_live_mode_is_still_refused():
    client = TestClient(__import__("app.main", fromlist=["app"]).app)
    refused = client.post(
        "/api/v1/runtime/start",
        headers={"Authorization": f"Bearer {get_settings().admin_token}"},
        json={"mode": "LIVE"},
    )
    assert refused.status_code == 400


def _book(db: Session, *, usd: Decimal = Decimal("10000"), risk: dict | None = None, prices: list[str] | None = None, volume: str = "1000000", lows: list[str] | None = None, highs: list[str] | None = None, opens: list[str] | None = None):
    symbol = f"V{uuid4().hex[:8]}".upper()
    account = create_account(
        db,
        name=f"v5a2-{uuid4().hex[:10]}",
        base_currency="JPY",
        jpy_cash=Decimal("0"),
        usd_cash=usd,
        markets=["US"],
        strategies=["buy_hold"],
        symbols=[symbol],
        mode="RULE_BASED",
        fallback_to_rules=False,
        risk_settings=risk or {"slippage_pct": "0", "spread_pct": "0"},
    )
    db.commit()
    _anchor(db, account, symbol)
    series = prices or ["100", "110"]
    bars = []
    for index, price in enumerate(series):
        bars.append(
            _bar(
                _at(index),
                price,
                symbol,
                open_price=None if opens is None else opens[index],
                high=None if highs is None else highs[index],
                low=None if lows is None else lows[index],
                volume=volume if index else "1000000",
            )
        )
    return account, symbol, bars


def _paper(db: Session) -> Account:
    account = db.scalar(select(Account).where(Account.name == "paper"))
    if account is not None:
        return account
    account = create_account(
        db,
        name="paper",
        base_currency="JPY",
        jpy_cash=Decimal("1000000"),
        usd_cash=Decimal("0"),
        markets=["US"],
        strategies=["ema_cross"],
        symbols=["AAPL"],
        mode="PAUSED",
        fallback_to_rules=False,
        risk_settings={},
    )
    db.commit()
    return account


def _anchor(db: Session, account, symbol: str) -> None:
    db.add(ProcessingCheckpoint(account_id=account.id, market="US", symbol=symbol, bar_timestamp=BAR - timedelta(minutes=5)))
    db.commit()


def _step(db: Session, account, bars, when: datetime):
    result = run_cycle(db, when + CLOSED, account_name=account.name, source=StaticSource(bars), delay_seconds=0, job_name=account.name)
    assert result.status == "SUCCEEDED", result.reason
    return result


def _orders(db: Session, account_id: int) -> list[OrderRecord]:
    return list(db.scalars(select(OrderRecord).where(OrderRecord.account_id == account_id).order_by(OrderRecord.id)).all())


def _assert_books(db: Session, account) -> None:
    rows = list(
        db.scalars(
            select(CashLedgerEntry).where(CashLedgerEntry.account_id == account.id, CashLedgerEntry.currency == "USD").order_by(CashLedgerEntry.id)
        ).all()
    )
    running = Decimal(0)
    for row in rows:
        running += row.amount
        assert row.balance_after == running
    assert cash_balances(db, account.id)["USD"] == rows[-1].balance_after
    buy_qty = Decimal(0)
    sell_qty = Decimal(0)
    for order in _orders(db, account.id):
        filled = db.scalar(select(func.coalesce(func.sum(ExecutionRecord.quantity), 0)).where(ExecutionRecord.order_id == order.id))
        if order.side == "BUY":
            buy_qty += filled
        else:
            sell_qty += filled
    open_qty = sum(
        (
            row.quantity
            for row in db.scalars(
                select(PositionRecord).where(
                    PositionRecord.account_id == account.id,
                    PositionRecord.backtest_run_id.is_(None),
                    PositionRecord.status == "OPEN",
                )
            ).all()
        ),
        Decimal(0),
    )
    assert open_qty == buy_qty - sell_qty


def _bar(timestamp: datetime, close: str, symbol: str, *, open_price: str | None, high: str | None, low: str | None, volume: str) -> NormalizedBar:
    close_price = Decimal(close)
    opened = Decimal(open_price or close)
    return NormalizedBar(
        market="US",
        symbol=symbol,
        timestamp=timestamp,
        timeframe="5m",
        open=opened,
        high=Decimal(high) if high is not None else max(opened, close_price),
        low=Decimal(low) if low is not None else min(opened, close_price),
        close=close_price,
        volume=Decimal(volume),
        provider="runtime",
    )


def _at(index: int) -> datetime:
    return BAR + timedelta(minutes=5 * index)
