from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select
import pytest
from sqlalchemy.orm import Session

from app.core.config import assert_phase1_safety, get_settings
from app.core.config import Settings
from app.data.bar import NormalizedBar
from app.db.session import SessionLocal
from app.models.runtime import NotificationEvent, ProcessingCheckpoint
from app.models.trading import CashLedgerEntry, ExecutionRecord, OrderRecord, PositionRecord
from app.runtime.accounts import cash_balances, create_account
from app.runtime.cycle import StaticSource, pending_expected, run_cycle
from app.runtime.marketdata import collect_market_data
from app.ai.calendar import session_bar_starts
from app.trading.indicators import ema

UTC = timezone.utc
CLOSED = timedelta(minutes=5, seconds=5)
STARTS = session_bar_starts("US", date(2024, 1, 2))


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


def _when(index: int) -> datetime:
    return STARTS[index] + CLOSED


def _bar(index: int, close: str, symbol: str, *, low: str | None = None, high: str | None = None, volume: str = "1000000") -> NormalizedBar:
    close_price = Decimal(close)
    return NormalizedBar(
        market="US",
        symbol=symbol,
        timestamp=STARTS[index],
        timeframe="5m",
        open=close_price,
        high=Decimal(high) if high is not None else close_price,
        low=Decimal(low) if low is not None else close_price,
        close=close_price,
        volume=Decimal(volume),
        provider="runtime",
    )


def _account(db: Session, symbols: list[str], risk: dict | None = None, *, strategies: list[str] | None = None, jpy: str = "1000000"):
    account = create_account(
        db,
        name=f"delay-{uuid4().hex[:10]}",
        base_currency="JPY",
        jpy_cash=Decimal(jpy),
        usd_cash=Decimal("10000"),
        markets=["US"],
        strategies=strategies or ["buy_hold"],
        symbols=symbols,
        mode="RULE_BASED",
        fallback_to_rules=False,
        risk_settings=risk or {"slippage_pct": "0", "spread_pct": "0", "stop_loss_pct": "0.05"},
    )
    db.commit()
    for symbol in symbols:
        db.add(ProcessingCheckpoint(account_id=account.id, market="US", symbol=symbol, bar_timestamp=STARTS[0] - timedelta(minutes=5)))
    db.commit()
    return account


def _run(db: Session, account, bars, index: int):
    result = run_cycle(
        db,
        _when(index),
        account_name=account.name,
        source=StaticSource(bars),
        delay_seconds=0,
        job_name=account.name,
    )
    assert result.status == "SUCCEEDED", result.reason
    return result


def _orders(db: Session, account_id: int) -> list[OrderRecord]:
    return list(db.scalars(select(OrderRecord).where(OrderRecord.account_id == account_id).order_by(OrderRecord.id)).all())


def _sells(db: Session, account_id: int) -> list[OrderRecord]:
    return [row for row in _orders(db, account_id) if row.side == "SELL"]


def _notes(db: Session, account_id: int, code: str) -> list[NotificationEvent]:
    return list(
        db.scalars(
            select(NotificationEvent).where(NotificationEvent.account_id == account_id, NotificationEvent.code == code)
        ).all()
    )


def _open(db: Session, account, symbol: str):
    _run(db, account, [_bar(0, "100", symbol)], 0)
    _run(db, account, [_bar(1, "100", symbol, low="100")], 1)
    held = db.scalar(select(PositionRecord).where(PositionRecord.account_id == account.id, PositionRecord.status == "OPEN"))
    assert held is not None
    assert held.stop_price == Decimal("95")
    return held


def test_intermediate_stop_survives_a_rebound(db: Session):
    symbol = f"D{uuid4().hex[:6]}".upper()
    account = _account(db, [symbol])
    _open(db, account, symbol)
    bars = [
        _bar(2, "100", symbol, low="90"),
        _bar(3, "101", symbol, low="96"),
        _bar(4, "102", symbol, low="97"),
    ]
    _run(db, account, bars, 4)
    sells = _sells(db, account.id)
    assert len(sells) == 1
    assert sells[0].created_at == STARTS[2]
    assert sells[0].decision_reason == "stop_loss"
    assert sells[0].status == "SUBMITTED"
    assert db.scalar(select(ExecutionRecord.id).where(ExecutionRecord.order_id == sells[0].id)) is None


def test_twelve_delayed_bars_are_managed_in_order(db: Session):
    symbol = f"D{uuid4().hex[:6]}".upper()
    account = _account(db, [symbol])
    _open(db, account, symbol)
    bars = []
    for index in range(2, 14):
        low = "90" if index == 5 else "96"
        bars.append(_bar(index, "100", symbol, low=low))
    _run(db, account, bars, 13)
    sells = _sells(db, account.id)
    assert len(sells) == 1
    assert sells[0].created_at == STARTS[5]
    marks = list(
        db.scalars(
            select(ProcessingCheckpoint.bar_timestamp)
            .where(
                ProcessingCheckpoint.account_id == account.id,
                ProcessingCheckpoint.bar_timestamp >= STARTS[2],
            )
            .order_by(ProcessingCheckpoint.bar_timestamp)
        ).all()
    )
    assert marks == STARTS[2:14]


def test_new_signal_is_evaluated_only_on_the_latest_bar(db: Session):
    symbol = f"D{uuid4().hex[:6]}".upper()
    closes = [Decimal(10)] * 4 + [Decimal(20)] * 4
    fast = ema(closes, 2)
    slow = ema(closes, 3)

    def crossed(index: int) -> bool:
        values = {fast[index], slow[index], fast[index - 1], slow[index - 1]}
        return None not in values and fast[index - 1] <= slow[index - 1] and fast[index] > slow[index]

    assert any(crossed(index) for index in range(1, len(closes) - 1))
    assert not crossed(len(closes) - 1)
    account = _account(db, [symbol], {"ema_fast": 2, "ema_slow": 3, "slippage_pct": "0", "spread_pct": "0"}, strategies=["ema_cross"])
    bars = [_bar(index, str(close), symbol) for index, close in enumerate(closes)]
    _run(db, account, bars, len(closes) - 1)
    assert _orders(db, account.id) == []


def test_fill_bar_does_not_protect_the_new_position(db: Session):
    symbol = f"D{uuid4().hex[:6]}".upper()
    account = _account(db, [symbol])
    _run(db, account, [_bar(0, "100", symbol)], 0)
    _run(db, account, [_bar(1, "100", symbol, low="90")], 1)
    assert _sells(db, account.id) == []
    held = db.scalar(select(PositionRecord).where(PositionRecord.account_id == account.id, PositionRecord.status == "OPEN"))
    assert held.stop_price == Decimal("95")


def test_the_next_bar_submits_the_protective_sell(db: Session):
    symbol = f"D{uuid4().hex[:6]}".upper()
    account = _account(db, [symbol])
    _run(db, account, [_bar(0, "100", symbol)], 0)
    _run(db, account, [_bar(1, "100", symbol, low="90")], 1)
    _run(db, account, [_bar(2, "100", symbol, low="90")], 2)
    sells = _sells(db, account.id)
    assert len(sells) == 1
    assert sells[0].created_at == STARTS[2]
    assert sells[0].status == "SUBMITTED"
    assert db.scalar(select(ExecutionRecord.id).where(ExecutionRecord.order_id == sells[0].id)) is None


def test_an_existing_position_is_protected_while_another_symbol_fills(db: Session):
    old = f"O{uuid4().hex[:6]}".upper()
    new = f"N{uuid4().hex[:6]}".upper()
    account = _account(db, [old])
    _open(db, account, old)
    account.symbols = [old, new]
    db.add(ProcessingCheckpoint(account_id=account.id, market="US", symbol=new, bar_timestamp=STARTS[1]))
    db.commit()
    _run(db, account, [_bar(2, "100", old, low="100"), _bar(2, "100", new)], 2)
    new_buys = [row for row in _orders(db, account.id) if row.side == "BUY" and row.created_at == STARTS[2]]
    assert len(new_buys) == 1
    assert new_buys[0].status == "SUBMITTED"
    _run(db, account, [_bar(3, "100", old, low="90"), _bar(3, "100", new, low="90")], 3)
    sells = _sells(db, account.id)
    assert len(sells) == 1
    assert sells[0].created_at == STARTS[3]
    assert db.get(OrderRecord, new_buys[0].id).status == "FILLED"


def test_stop_wins_when_stop_and_target_hit_the_same_bar(db: Session):
    symbol = f"D{uuid4().hex[:6]}".upper()
    account = _account(db, [symbol], {"slippage_pct": "0", "spread_pct": "0", "stop_loss_pct": "0.03", "take_profit_pct": "0.05"})
    _run(db, account, [_bar(0, "100", symbol)], 0)
    _run(db, account, [_bar(1, "100", symbol)], 1)
    held = db.scalar(select(PositionRecord).where(PositionRecord.account_id == account.id, PositionRecord.status == "OPEN"))
    assert held.stop_price == Decimal("97")
    assert held.take_profit == Decimal("105")
    _run(db, account, [_bar(2, "100", symbol, low="96", high="106")], 2)
    sells = _sells(db, account.id)
    assert len(sells) == 1
    assert sells[0].decision_reason == "stop_loss"
    assert sells[0].status == "SUBMITTED"


def test_fill_requires_the_bar_to_end_after_submission(db: Session):
    symbol = f"D{uuid4().hex[:6]}".upper()
    account = _account(db, [symbol], {"slippage_pct": "0", "spread_pct": "0", "stop_loss_pct": "0.5"})
    _run(db, account, [_bar(0, "100", symbol)], 0)
    order = _orders(db, account.id)[0]
    order.submitted_at = STARTS[2]
    db.commit()
    _run(db, account, [_bar(1, "101", symbol), _bar(2, "110", symbol)], 2)
    execution = db.scalar(select(ExecutionRecord).where(ExecutionRecord.order_id == order.id))
    assert execution.price == Decimal("110")
    assert execution.fill_observed_at == STARTS[2] + timedelta(minutes=5)
    assert execution.executed_at == STARTS[2]


def test_a_previous_session_gap_is_backfilled_from_the_checkpoint(db: Session):
    symbol = f"D{uuid4().hex[:6]}".upper()
    account = _account(db, [symbol], {"slippage_pct": "0", "spread_pct": "0", "stop_loss_pct": "0.5"})
    tuesday = session_bar_starts("US", date(2024, 1, 2))
    wednesday = session_bar_starts("US", date(2024, 1, 3))
    db.add(ProcessingCheckpoint(account_id=account.id, market="US", symbol=symbol, bar_timestamp=tuesday[0]))
    db.commit()
    now = wednesday[0] + CLOSED

    class Recorder:
        def __init__(self):
            self.start = None

        def fetch(self, market, symbol, start, end):
            self.start = start
            return []

    recorder = Recorder()
    collect_market_data(db, now, source=recorder, symbols=[("US", symbol)], account_name=account.name)
    assert recorder.start == tuesday[1]

    later = [_bar_at(item, "100", symbol) for item in tuesday[2:] + wednesday[:1]]
    blocked = run_cycle(db, now, account_name=account.name, source=StaticSource(later), delay_seconds=0, job_name=account.name)
    assert blocked.status == "SUCCEEDED", blocked.reason
    assert _orders(db, account.id) == []
    assert db.scalar(
        select(ProcessingCheckpoint.id).where(
            ProcessingCheckpoint.account_id == account.id,
            ProcessingCheckpoint.bar_timestamp == wednesday[0],
        )
    ) is None
    complete = [_bar_at(item, "100", symbol) for item in tuesday[1:] + wednesday[:1]]
    restored = run_cycle(db, now, account_name=account.name, source=StaticSource(complete), delay_seconds=0, job_name=account.name)
    assert restored.status == "SUCCEEDED", restored.reason
    orders = _orders(db, account.id)
    assert len(orders) == 1
    assert orders[0].created_at == wednesday[0]
    assert db.scalar(
        select(ProcessingCheckpoint.id).where(
            ProcessingCheckpoint.account_id == account.id,
            ProcessingCheckpoint.bar_timestamp == tuesday[1],
        )
    ) is not None


def test_weekends_holidays_and_early_closes_are_not_gaps():
    friday = session_bar_starts("US", date(2026, 11, 13))
    monday = session_bar_starts("US", date(2026, 11, 16))
    weekend = pending_expected("US", friday[-1], monday[0] + CLOSED, 0)
    assert weekend == [monday[0]]
    assert all(item.astimezone(timezone(timedelta(hours=-5))).weekday() < 5 for item in weekend)

    wednesday = session_bar_starts("US", date(2026, 11, 25))
    holiday_friday = session_bar_starts("US", date(2026, 11, 27))
    across_holiday = pending_expected("US", wednesday[-1], holiday_friday[0] + CLOSED, 0)
    assert across_holiday[0] == holiday_friday[0]
    assert all(item.astimezone(timezone(timedelta(hours=-5))).date() != date(2026, 11, 26) for item in across_holiday)

    early = session_bar_starts("US", date(2026, 11, 27))
    pending = pending_expected("US", early[0], early[-1] + CLOSED, 0)
    assert pending[-1] == early[-1]
    assert all(item.astimezone(timezone(timedelta(hours=-5))).hour < 13 for item in pending)
    assert session_bar_starts("US", date(2026, 11, 26)) == []


def test_a_gap_blocks_new_buys_and_bars_after_it(db: Session):
    symbol = f"D{uuid4().hex[:6]}".upper()
    account = _account(db, [symbol])
    db.add(ProcessingCheckpoint(account_id=account.id, market="US", symbol=symbol, bar_timestamp=STARTS[0]))
    db.commit()
    bars = [_bar(index, "100", symbol) for index in (2, 3, 4)]
    result = _run(db, account, bars, 4)
    assert _orders(db, account.id) == []
    assert f"{symbol}:gap" in result.processed
    assert db.scalar(
        select(ProcessingCheckpoint.id).where(
            ProcessingCheckpoint.account_id == account.id,
            ProcessingCheckpoint.bar_timestamp == STARTS[2],
        )
    ) is None
    assert len(_notes(db, account.id, "data_gap")) == 1


def test_the_same_gap_notification_is_not_repeated(db: Session):
    symbol = f"D{uuid4().hex[:6]}".upper()
    account = _account(db, [symbol])
    db.add(ProcessingCheckpoint(account_id=account.id, market="US", symbol=symbol, bar_timestamp=STARTS[0]))
    db.commit()
    bars = [_bar(index, "100", symbol) for index in (2, 3, 4)]
    _run(db, account, bars, 4)
    _run(db, account, bars, 4)
    assert len(_notes(db, account.id, "data_gap")) == 1


def test_reprocessing_does_not_duplicate_orders_or_fills(db: Session):
    symbol = f"D{uuid4().hex[:6]}".upper()
    account = _account(db, [symbol])
    _open(db, account, symbol)
    bars = [_bar(2, "100", symbol, low="90"), _bar(3, "101", symbol, low="96"), _bar(4, "102", symbol, low="97")]
    _run(db, account, bars, 4)
    again = run_cycle(db, _when(4), account_name=account.name, source=StaticSource(bars), delay_seconds=0, job_name=account.name)
    assert again.status == "SKIPPED"
    sells = _sells(db, account.id)
    assert len(sells) == 1
    assert db.scalar(select(func.count()).select_from(ExecutionRecord).where(ExecutionRecord.order_id == sells[0].id)) == 0
    assert len(_orders(db, account.id)) == 2


def test_partial_remainder_fills_on_the_next_bar_without_changing_jpy(db: Session):
    symbol = f"D{uuid4().hex[:6]}".upper()
    account = _account(
        db,
        [symbol],
        {"slippage_pct": "0.0005", "spread_pct": "0.0002", "stop_loss_pct": "0.5", "participation_rate": "0.5"},
    )
    jpy_before = cash_balances(db, account.id)["JPY"]
    _run(db, account, [_bar(0, "100", symbol, volume="1")], 0)
    _run(db, account, [_bar(1, "100", symbol, volume="1"), _bar(2, "100", symbol, volume="1")], 2)
    order = next(row for row in _orders(db, account.id) if row.side == "BUY")
    fills = list(db.scalars(select(ExecutionRecord).where(ExecutionRecord.order_id == order.id).order_by(ExecutionRecord.sequence)).all())
    assert len(fills) == 2
    assert fills[0].quantity == Decimal("0.5")
    assert fills[1].quantity == order.quantity - fills[0].quantity
    assert [fill.executed_at for fill in fills] == [STARTS[1], STARTS[2]]
    assert order.status == "FILLED"
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
    assert cash_balances(db, account.id)["JPY"] == jpy_before


def test_more_than_three_missing_bars_are_risk_unobservable(db: Session):
    symbol = f"D{uuid4().hex[:6]}".upper()
    account = _account(db, [symbol])
    db.add(ProcessingCheckpoint(account_id=account.id, market="US", symbol=symbol, bar_timestamp=STARTS[0]))
    db.commit()
    result = _run(db, account, [], 5)
    assert f"{symbol}:gap" in result.processed
    assert len(_notes(db, account.id, "data_gap")) == 1
    assert len(_notes(db, account.id, "risk_unobservable")) == 1
    _run(db, account, [], 5)
    assert len(_notes(db, account.id, "risk_unobservable")) == 1
    assert _orders(db, account.id) == []


def test_an_add_on_uses_the_stop_from_the_start_of_the_bar(db: Session):
    symbol = f"D{uuid4().hex[:6]}".upper()
    account = _account(
        db,
        [symbol],
        {"slippage_pct": "0", "spread_pct": "0", "stop_loss_pct": "0.10", "participation_rate": "0.5"},
    )
    _run(db, account, [_bar(0, "100", symbol, volume="1")], 0)
    _run(db, account, [_bar(1, "100", symbol, volume="1", low="100"), _bar(2, "110", symbol, volume="1", low="95")], 2)
    assert _sells(db, account.id) == []
    held = db.scalar(select(PositionRecord).where(PositionRecord.account_id == account.id, PositionRecord.status == "OPEN"))
    assert held.quantity > Decimal("0.5")


def _risk_message(symbol: str, count: int, missing: datetime) -> str:
    return f"{symbol} の期待足が {count} 本欠けています。最初の欠足は {missing.isoformat()} です。新規買いは停止します。"


def _gap_message(symbol: str, missing: datetime) -> str:
    return f"{symbol} の {missing.isoformat()} が欠足です。価格は補完せず、この足より後は処理しません。"


def _checkpoint(db: Session, account_id: int, symbol: str, when: datetime) -> None:
    db.add(ProcessingCheckpoint(account_id=account_id, market="US", symbol=symbol, bar_timestamp=when))
    db.commit()


def test_the_same_unobservable_gap_is_not_notified_twice(db: Session):
    symbol = f"D{uuid4().hex[:6]}".upper()
    account = _account(db, [symbol])
    _checkpoint(db, account.id, symbol, STARTS[0])
    _run(db, account, [], 4)
    _run(db, account, [], 4)
    notes = _notes(db, account.id, "risk_unobservable")
    assert [note.message for note in notes] == [_risk_message(symbol, 4, STARTS[1])]


def test_a_later_gap_with_the_same_count_is_a_new_notification(db: Session):
    symbol = f"D{uuid4().hex[:6]}".upper()
    account = _account(db, [symbol])
    _checkpoint(db, account.id, symbol, STARTS[0])
    _run(db, account, [], 4)
    _checkpoint(db, account.id, symbol, STARTS[5])
    _run(db, account, [], 9)
    messages = {note.message for note in _notes(db, account.id, "risk_unobservable")}
    assert messages == {_risk_message(symbol, 4, STARTS[1]), _risk_message(symbol, 4, STARTS[6])}


def test_the_same_gap_on_another_symbol_is_a_separate_notification(db: Session):
    first = f"A{uuid4().hex[:6]}".upper()
    second = f"B{uuid4().hex[:6]}".upper()
    account = _account(db, [first, second])
    _checkpoint(db, account.id, first, STARTS[0])
    _checkpoint(db, account.id, second, STARTS[0])
    _run(db, account, [], 4)
    messages = {note.message for note in _notes(db, account.id, "risk_unobservable")}
    assert messages == {_risk_message(first, 4, STARTS[1]), _risk_message(second, 4, STARTS[1])}


def test_three_missing_bars_stay_below_the_unobservable_threshold(db: Session):
    symbol = f"D{uuid4().hex[:6]}".upper()
    account = _account(db, [symbol])
    _checkpoint(db, account.id, symbol, STARTS[0])
    _run(db, account, [], 3)
    assert _notes(db, account.id, "risk_unobservable") == []
    _run(db, account, [], 4)
    notes = _notes(db, account.id, "risk_unobservable")
    assert [note.message for note in notes] == [_risk_message(symbol, 4, STARTS[1])]


def test_data_gap_text_and_dedupe_stay_unchanged(db: Session):
    symbol = f"D{uuid4().hex[:6]}".upper()
    account = _account(db, [symbol])
    _checkpoint(db, account.id, symbol, STARTS[0])
    bars = [_bar(index, "100", symbol) for index in (2, 3, 4)]
    _run(db, account, bars, 4)
    _run(db, account, bars, 4)
    notes = _notes(db, account.id, "data_gap")
    assert [note.message for note in notes] == [_gap_message(symbol, STARTS[1])]
    assert _notes(db, account.id, "risk_unobservable") == []


def test_live_mode_is_still_refused():
    client = TestClient(__import__("app.main", fromlist=["app"]).app)
    refused = client.post(
        "/api/v1/runtime/start",
        headers={"Authorization": f"Bearer {get_settings().admin_token}"},
        json={"mode": "LIVE"},
    )
    assert refused.status_code == 400
    try:
        assert_phase1_safety(Settings(trading_mode="LIVE"))
    except RuntimeError as exc:
        assert "LIVE" in str(exc)
    else:
        raise AssertionError("LIVE mode was accepted")


def _bar_at(timestamp: datetime, close: str, symbol: str) -> NormalizedBar:
    close_price = Decimal(close)
    return NormalizedBar(
        market="US",
        symbol=symbol,
        timestamp=timestamp,
        timeframe="5m",
        open=close_price,
        high=close_price,
        low=close_price,
        close=close_price,
        volume=Decimal("1000000"),
        provider="runtime",
    )
