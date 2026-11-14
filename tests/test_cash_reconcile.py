from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.data.bar import NormalizedBar
from app.models.trading import CashLedgerEntry, ExecutionRecord, OrderRecord, PositionRecord
from app.portfolio.ledger import Portfolio, money
from app.db.session import SessionLocal
from app.runtime.accounts import cash_balances, create_account
from app.runtime.cycle import StaticSource, _reconcile, run_cycle
from app.runtime.fills import paper_fill_quote
from app.trading.config import load_trading_config
from tests.test_phase5a2 import _anchor, _assert_books, _at, _bar, _orders


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

UTC = timezone.utc
CLOSED = timedelta(minutes=5, seconds=5)
FRACTIONAL = {
    "slippage_pct": "0.0005",
    "spread_pct": "0.0002",
}


def test_buy_cost_is_stored_at_ledger_scale():
    """The first production failure: 345.75 filled 5.7833 shares from 10,000 USD."""
    price = Decimal("345.957450000000")
    quantity = Decimal("5.78330000")
    raw = Decimal("10000") - price * quantity
    assert raw != money(raw)
    book = Portfolio({"USD": Decimal("10000"), "JPY": Decimal("1000000")})
    book.apply_buy("AAPL", "USD", quantity, price, Decimal(0), datetime(2026, 10, 14, 13, 40, tzinfo=UTC), None, None)
    assert book.cash["USD"] == money(book.cash["USD"])
    assert book.cash["USD"] != raw
    assert book.ledger[-1].amount == money(-(price * quantity))
    assert book.cash["USD"] == Decimal("10000") + book.ledger[-1].amount
    assert book.ledger[-1].balance_after == book.cash["USD"]
    assert book.cash["JPY"] == Decimal("1000000")


def test_fractional_buy_commits_and_matches_the_ledger(db: Session):
    account, _symbol, bars = _fractional_book(db, prices=["344.79998779", "344.79998779"])
    jpy_before = cash_balances(db, account.id)["JPY"]
    _succeed(db, account, bars, _at(0))
    _succeed(db, account, bars, _at(1))
    order = _orders(db, account.id)[0]
    assert order.status == "FILLED"
    execution = db.scalar(select(ExecutionRecord).where(ExecutionRecord.order_id == order.id))
    assert execution.fill_observed_at > order.submitted_at
    _assert_money(db, account, jpy_before)
    again = run_cycle(
        db,
        _at(1) + CLOSED,
        account_name=account.name,
        source=StaticSource(bars),
        delay_seconds=0,
        job_name=account.name,
    )
    assert again.status == "SKIPPED"
    assert _fill_count(db, order.id) == 1
    assert _ledger_count(db, account.id, "BUY") == 1


def test_fractional_sell_commits_and_matches_the_ledger(db: Session):
    account, _symbol, bars = _fractional_book(
        db,
        prices=["345.75", "346.10", "347.20", "348.30", "349.40"],
    )
    jpy_before = cash_balances(db, account.id)["JPY"]
    _succeed(db, account, bars, _at(0))
    _succeed(db, account, bars, _at(1))
    account.strategies = ["rsi_reversion"]
    account.risk_settings = {
        **FRACTIONAL,
        "stop_loss_pct": "0.5",
        "rsi_period": 2,
        "rsi_exit": "70",
    }
    db.commit()
    _succeed(db, account, bars, _at(2))
    _succeed(db, account, bars, _at(3))
    sell = next(row for row in _orders(db, account.id) if row.side == "SELL")
    assert sell.status == "SUBMITTED"
    _succeed(db, account, bars, _at(4))
    assert sell.status == "FILLED"
    closed = db.scalar(select(PositionRecord).where(PositionRecord.account_id == account.id, PositionRecord.status == "CLOSED"))
    assert closed.quantity == 0
    _assert_money(db, account, jpy_before)


def test_fractional_partial_fill_matches_position_and_cash(db: Session):
    account, _symbol, bars = _fractional_book(
        db,
        prices=["344.79998779", "344.79998779"],
        risk={**FRACTIONAL, "participation_rate": "0.5"},
        volume="11.4128",
    )
    jpy_before = cash_balances(db, account.id)["JPY"]
    _succeed(db, account, bars, _at(0))
    _succeed(db, account, bars, _at(1))
    order = _orders(db, account.id)[0]
    assert order.status == "PARTIALLY_FILLED"
    assert order.filled_quantity == Decimal("5.7064")
    position = db.scalar(select(PositionRecord).where(PositionRecord.account_id == account.id, PositionRecord.status == "OPEN"))
    assert position.quantity == order.filled_quantity
    _assert_money(db, account, jpy_before)


def test_flush_failure_after_a_fill_does_not_double_book(db: Session):
    account, _symbol, bars = _fractional_book(db, prices=["344.79998779", "344.79998779"])
    _succeed(db, account, bars, _at(0))
    order = _orders(db, account.id)[0]
    usd_before = cash_balances(db, account.id)["USD"]
    seen = {"filled": False}

    def before_flush(session, _flush_context, _instances):
        if any(isinstance(row, ExecutionRecord) for row in session.new) or any(
            isinstance(row, CashLedgerEntry) and row.reason == "BUY" for row in session.new
        ):
            seen["filled"] = True

    def after_flush(_session, _flush_context):
        if seen["filled"]:
            raise RuntimeError("injected failure after the fill and cash ledger were staged")

    event.listen(db, "before_flush", before_flush)
    event.listen(db, "after_flush", after_flush)
    try:
        failed = run_cycle(
            db,
            _at(1) + CLOSED,
            account_name=account.name,
            source=StaticSource(bars),
            delay_seconds=0,
            job_name=account.name,
        )
    finally:
        event.remove(db, "before_flush", before_flush)
        event.remove(db, "after_flush", after_flush)
    assert failed.status == "FAILED"
    assert failed.reason == "injected failure after the fill and cash ledger were staged"
    db.expire_all()
    assert db.get(OrderRecord, order.id).status == "SUBMITTED"
    assert _fill_count(db, order.id) == 0
    assert cash_balances(db, account.id)["USD"] == usd_before
    _succeed(db, account, bars, _at(1))
    assert db.get(OrderRecord, order.id).status == "FILLED"
    assert _fill_count(db, order.id) == 1
    assert _ledger_count(db, account.id, "BUY") == 1
    _assert_books(db, account)


def test_reload_restores_an_unfilled_order_and_fills_it_once(db: Session):
    account, _symbol, bars = _fractional_book(db, prices=["344.79998779", "344.79998779"])
    _succeed(db, account, bars, _at(0))
    order = _orders(db, account.id)[0]
    assert order.status == "SUBMITTED"
    db.expire_all()
    _succeed(db, account, bars, _at(1))
    assert db.get(OrderRecord, order.id).status == "FILLED"
    assert _fill_count(db, order.id) == 1


def test_canceled_order_does_not_fill_on_the_next_bar(db: Session):
    account, _symbol, bars = _fractional_book(db, prices=["344.79998779", "344.79998779"])
    _succeed(db, account, bars, _at(0))
    order = _orders(db, account.id)[0]
    client = TestClient(__import__("app.main", fromlist=["app"]).app)
    canceled = client.post(
        f"/api/v1/accounts/{account.id}/orders/{order.id}/cancel",
        headers={"Authorization": f"Bearer {get_settings().admin_token}"},
    )
    assert canceled.status_code == 200
    _succeed(db, account, bars, _at(1))
    db.expire_all()
    assert db.get(OrderRecord, order.id).status == "CANCELED"
    assert _fill_count(db, order.id) == 0
    assert cash_balances(db, account.id)["USD"] == Decimal("10000")


def test_paused_account_does_not_submit_a_new_buy(db: Session):
    account, _symbol, bars = _fractional_book(db, prices=["344.79998779"])
    account.mode = "PAUSED"
    db.commit()
    result = _succeed(db, account, bars, _at(0))
    assert result.orders == 0
    assert _orders(db, account.id) == []


def test_reconcile_still_rejects_a_real_cash_mismatch(db: Session):
    account, _symbol, _bars = _fractional_book(db, prices=["344.79998779"])
    book = Portfolio({"USD": Decimal("1"), "JPY": cash_balances(db, account.id)["JPY"]})
    try:
        _reconcile(db, account.id, book)
    except RuntimeError as exc:
        assert str(exc) == "cash reconciliation failed for USD"
    else:
        raise AssertionError("a real cash mismatch was accepted")


def test_order_148_quote_is_after_submission_and_cash_rounds():
    config = load_trading_config()
    submitted = datetime(2026, 10, 30, 16, 50, 30, 5560, tzinfo=UTC)
    bar_start = datetime(2026, 10, 30, 19, 55, tzinfo=UTC)
    close = Decimal("344.79998779")
    quote = paper_fill_quote(
        side="BUY",
        submitted_at=submitted,
        bar_start=bar_start,
        open_price=Decimal("342.64001465"),
        close_price=close,
        received_at=bar_start + timedelta(minutes=5, seconds=30),
        spread_pct=config.spread_pct,
        slippage_pct=config.slippage_pct,
    )
    assert quote is not None
    assert quote.observed_at > submitted
    raw = Decimal("9923.65583567") - quote.price * Decimal("5.70640000")
    assert raw != money(raw)
    book = Portfolio({"USD": Decimal("9923.65583567")})
    book.apply_buy("AAPL", "USD", Decimal("5.70640000"), quote.price, Decimal(0), bar_start, None, None)
    assert book.cash["USD"] == money(book.cash["USD"])
    assert book.cash["USD"] == Decimal("9923.65583567") + book.ledger[-1].amount
    assert book.ledger[-1].balance_after == book.cash["USD"]


def _fractional_book(db: Session, *, prices: list[str], risk: dict | None = None, volume: str = "1000000"):
    symbol = f"C{uuid4().hex[:8]}".upper()
    account = create_account(
        db,
        name=f"cash-{uuid4().hex[:10]}",
        base_currency="JPY",
        jpy_cash=Decimal("1000000"),
        usd_cash=Decimal("10000"),
        markets=["US"],
        strategies=["buy_hold"],
        symbols=[symbol],
        mode="RULE_BASED",
        fallback_to_rules=False,
        risk_settings=risk or FRACTIONAL,
    )
    db.commit()
    _anchor(db, account, symbol)
    bars = [_bar(_at(index), price, symbol, open_price=price, high=price, low=price, volume=volume if index else "1000000") for index, price in enumerate(prices)]
    return account, symbol, bars


def _succeed(db: Session, account, bars, when: datetime):
    result = run_cycle(
        db,
        when + CLOSED,
        account_name=account.name,
        source=StaticSource(bars),
        delay_seconds=0,
        job_name=account.name,
    )
    assert result.status == "SUCCEEDED", result.reason
    return result


def _assert_money(db: Session, account, jpy_before: Decimal) -> None:
    _assert_books(db, account)
    assert cash_balances(db, account.id)["JPY"] == jpy_before
    usd = cash_balances(db, account.id)["USD"]
    assert usd == money(usd)


def _fill_count(db: Session, order_id: int) -> int:
    return int(db.scalar(select(func.count()).select_from(ExecutionRecord).where(ExecutionRecord.order_id == order_id)) or 0)


def _ledger_count(db: Session, account_id: int, reason: str) -> int:
    return int(
        db.scalar(
            select(func.count()).select_from(CashLedgerEntry).where(
                CashLedgerEntry.account_id == account_id,
                CashLedgerEntry.reason == reason,
            )
        )
        or 0
    )
