from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.backtest.engine import InsufficientDataError, run_backtest
from app.backtest.metrics import compute_metrics
from app.broker.simulated import SimulatedBroker
from app.core.config import Settings, assert_phase1_safety
from app.portfolio.ledger import Portfolio
from app.risk.manager import RiskManager
from app.trading.config import TradingConfig
from app.trading.indicators import ema, rsi
from app.trading.strategies import BuyHoldStrategy, EmaCrossStrategy, PriceBar, RsiReversionStrategy, Signal, Strategy

UTC = timezone.utc
START = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)


def _config(**overrides) -> TradingConfig:
    values = dict(
        slippage_pct=Decimal("0"),
        spread_pct=Decimal("0"),
        commission_pct=Decimal("0"),
        stop_loss_pct=Decimal("0.5"),
        take_profit_pct=None,
        risk_per_trade=Decimal("1"),
        daily_loss_pct=Decimal("0.99"),
        max_drawdown_pct=Decimal("0.99"),
        max_position_pct=Decimal("1"),
        max_positions=5,
        participation_rate=Decimal("1"),
    )
    values.update(overrides)
    return TradingConfig(**values)


def _bar(symbol: str, index: int, close: str, *, open_: str | None = None, high: str | None = None, low: str | None = None, volume: str = "1000", start: datetime = START) -> PriceBar:
    close_price = Decimal(close)
    open_price = Decimal(open_) if open_ is not None else close_price
    return PriceBar(
        symbol=symbol,
        timestamp=start + timedelta(minutes=5 * index),
        open=open_price,
        high=Decimal(high) if high is not None else max(open_price, close_price) + Decimal("0.2"),
        low=Decimal(low) if low is not None else min(open_price, close_price) - Decimal("0.2"),
        close=close_price,
        volume=Decimal(volume),
    )


def _series(symbol: str, closes: list[Decimal], **kwargs) -> list[PriceBar]:
    return [_bar(symbol, index, format(price, "f"), **kwargs) for index, price in enumerate(closes)]


class _RoundTrip(Strategy):
    name = "round_trip"
    version = "test"

    @property
    def minimum_bars(self) -> int:
        return 1

    def evaluate(self, bars: list[PriceBar]) -> Signal | None:
        if len(bars) == 1:
            return Signal(bars[-1].symbol, bars[-1].timestamp, "BUY", "enter")
        if len(bars) == 3:
            return Signal(bars[-1].symbol, bars[-1].timestamp, "SELL", "exit")
        return None


class _FirstBuy(Strategy):
    name = "first_buy"
    version = "test"

    @property
    def minimum_bars(self) -> int:
        return 1

    def evaluate(self, bars: list[PriceBar]) -> Signal | None:
        if len(bars) == 1:
            return Signal(bars[-1].symbol, bars[-1].timestamp, "BUY", "first")
        return None


class _CrashThenOther(Strategy):
    name = "crash"
    version = "test"

    @property
    def minimum_bars(self) -> int:
        return 1

    def evaluate(self, bars: list[PriceBar]) -> Signal | None:
        if bars[-1].symbol == "AAA" and len(bars) == 1:
            return Signal("AAA", bars[-1].timestamp, "BUY", "enter")
        if bars[-1].symbol == "BBB" and len(bars) == 3:
            return Signal("BBB", bars[-1].timestamp, "BUY", "late")
        return None


def test_ema_cross_matches_the_causal_indicator():
    closes = [Decimal(10)] * 6 + [Decimal(11), Decimal(12), Decimal(13)]
    bars = _series("AAPL", closes)
    fast = ema(closes, 3)
    slow = ema(closes, 5)
    expected = next(
        index
        for index in range(1, len(closes))
        if None not in {fast[index], slow[index], fast[index - 1], slow[index - 1]}
        and fast[index - 1] <= slow[index - 1]
        and fast[index] > slow[index]
    )
    strategy = EmaCrossStrategy(_config(ema_fast=3, ema_slow=5))
    signal = strategy.evaluate(bars[: expected + 1])
    assert signal is not None
    assert signal.side == "BUY"
    assert signal.timestamp == bars[expected].timestamp
    assert strategy.evaluate(bars[:expected]) is None


def test_rsi_recovery_and_exit_match_the_causal_indicator():
    closes = [Decimal(value) for value in [100, 90, 80, 70, 60, 50, 40, 45, 55, 70]]
    bars = _series("AAPL", closes)
    values = rsi(closes, 3)
    entry = next(index for index in range(1, len(values)) if values[index - 1] is not None and values[index - 1] < 30 <= values[index])
    exit_at = next(index for index in range(len(values)) if values[index] is not None and values[index] >= 70)
    strategy = RsiReversionStrategy(_config(rsi_period=3, rsi_entry=Decimal(30), rsi_exit=Decimal(70)))
    buy = strategy.evaluate(bars[: entry + 1])
    sell = RsiReversionStrategy(_config(rsi_period=3)).evaluate(bars[: exit_at + 1])
    assert buy is not None and buy.side == "BUY" and buy.timestamp == bars[entry].timestamp
    assert sell is not None and sell.side == "SELL" and sell.timestamp == bars[exit_at].timestamp


def test_indicators_ignore_future_bars():
    closes = [Decimal(value) for value in [100, 90, 80, 70, 60, 50, 40, 45, 55, 70, 90]]
    cut = 9
    assert ema(closes, 3)[cut - 1] == ema(closes[:cut], 3)[-1]
    assert rsi(closes, 3)[cut - 1] == rsi(closes[:cut], 3)[-1]
    rising = [Decimal(10)] * 6 + [Decimal(11), Decimal(12), Decimal(13), Decimal(14)]
    bars = _series("AAPL", rising)
    decision = bars[:7]
    early = EmaCrossStrategy(_config(ema_fast=3, ema_slow=5)).evaluate(decision)
    assert ema(rising, 3)[6] == ema(rising[:7], 3)[-1]
    assert early is not None and early.side == "BUY" and early.timestamp == decision[-1].timestamp


def test_signal_fills_on_the_next_bar_open():
    bars = [
        _bar("AAPL", 0, "100", open_="100"),
        _bar("AAPL", 1, "99", open_="99"),
    ]
    output = run_backtest(
        {"AAPL": bars},
        {"AAPL": "USD"},
        BuyHoldStrategy(_config()),
        _config(),
        "US",
        {"USD": Decimal("10000")},
    )
    assert len(output.fills) == 1
    fill = output.fills[0]
    assert fill.signal_time == bars[0].timestamp
    assert fill.executed_at == bars[1].timestamp
    assert fill.price == Decimal("99")
    assert fill.price != bars[0].close


def test_insufficient_cash_rejects_the_order_without_a_fill():
    portfolio = Portfolio({"USD": Decimal("100")})
    broker = SimulatedBroker(portfolio, _config())
    now = START
    order = broker.submit_market(
        symbol="AAPL",
        side="BUY",
        quantity=Decimal("10"),
        currency="USD",
        execute_at=now,
        idempotency_key="cash",
        reserve_price=Decimal("50"),
        created_at=now,
    )
    bar = _bar("AAPL", 0, "50", open_="50")
    assert broker.fill_market(order, bar) is None
    assert order.status == "REJECTED"
    assert broker.executions == []
    assert portfolio.cash["USD"] == Decimal("100")


def test_duplicate_order_and_duplicate_fill_are_ignored():
    portfolio = Portfolio({"USD": Decimal("1000")})
    broker = SimulatedBroker(portfolio, _config())
    now = START
    first = broker.submit_market(
        symbol="AAPL",
        side="BUY",
        quantity=Decimal("1"),
        currency="USD",
        execute_at=now,
        idempotency_key="same",
        reserve_price=Decimal("10"),
        created_at=now,
    )
    second = broker.submit_market(
        symbol="AAPL",
        side="BUY",
        quantity=Decimal("1"),
        currency="USD",
        execute_at=now,
        idempotency_key="same",
        reserve_price=Decimal("10"),
        created_at=now,
    )
    bar = _bar("AAPL", 0, "10", open_="10")
    assert first.id == second.id
    assert len(broker.orders) == 1
    assert broker.fill_market(first, bar) is not None
    assert broker.fill_market(first, bar) is None
    assert len(broker.executions) == 1
    assert portfolio.positions["AAPL"].quantity == Decimal("1")


def test_partial_fill_uses_bar_volume_and_does_not_fill_twice_on_that_bar():
    portfolio = Portfolio({"USD": Decimal("100000")})
    broker = SimulatedBroker(portfolio, _config(participation_rate=Decimal("0.25")))
    now = START
    order = broker.submit_market(
        symbol="AAPL",
        side="BUY",
        quantity=Decimal("100"),
        currency="USD",
        execute_at=now,
        idempotency_key="partial",
        reserve_price=Decimal("10"),
        created_at=now,
    )
    bar = _bar("AAPL", 0, "10", open_="10", volume="100")
    fill = broker.fill_market(order, bar)
    assert fill is not None
    assert fill.quantity == Decimal("25")
    assert order.status == "PARTIALLY_FILLED"
    assert broker.fill_market(order, bar) is None
    assert len(broker.executions) == 1


def test_cancel_releases_reserved_cash_and_prevents_a_fill():
    portfolio = Portfolio({"USD": Decimal("1000")})
    broker = SimulatedBroker(portfolio, _config())
    order = broker.submit_limit(
        symbol="AAPL",
        side="BUY",
        quantity=Decimal("2"),
        currency="USD",
        limit_price=Decimal("100"),
        idempotency_key="limit",
        created_at=START,
    )
    assert portfolio.available("USD") < Decimal("1000")
    broker.cancel(order.id)
    assert order.status == "CANCELED"
    assert portfolio.available("USD") == Decimal("1000")
    assert broker.fill_limits(_bar("AAPL", 0, "90", open_="90", low="89")) == []


def test_round_trip_pnl_uses_entry_and_exit_prices():
    bars = [
        _bar("AAPL", 0, "100", open_="100"),
        _bar("AAPL", 1, "100", open_="100"),
        _bar("AAPL", 2, "100", open_="100"),
        _bar("AAPL", 3, "110", open_="110"),
    ]
    output = run_backtest({"AAPL": bars}, {"AAPL": "USD"}, _RoundTrip(), _config(), "US", {"USD": Decimal("10000")})
    sells = [fill for fill in output.fills if fill.side == "SELL"]
    assert len(sells) == 1
    assert sells[0].price == Decimal("110")
    assert sells[0].realized_pnl == Decimal("1000")
    assert Decimal(output.metrics["total_pnl"]) == Decimal("1000")
    assert output.metrics["win_rate"] == "1"
    assert output.metrics["profit_factor"] is None
    assert output.metrics["average_loss"] is None


def test_commission_slippage_and_spread_are_charged_on_the_fill():
    config = _config(slippage_pct=Decimal("0.01"), spread_pct=Decimal("0.002"), commission_pct=Decimal("0.001"))
    portfolio = Portfolio({"USD": Decimal("100000")})
    broker = SimulatedBroker(portfolio, config)
    order = broker.submit_market(
        symbol="AAPL",
        side="BUY",
        quantity=Decimal("2"),
        currency="USD",
        execute_at=START,
        idempotency_key="costs",
        reserve_price=Decimal("100"),
        created_at=START,
    )
    fill = broker.fill_market(order, _bar("AAPL", 0, "100", open_="100"))
    assert fill is not None
    assert fill.price == Decimal("101.1")
    assert fill.slippage_cost == Decimal("2.2")
    assert fill.spread_cost == Decimal("0.2")
    assert fill.commission == Decimal("101.1") * Decimal("2") * Decimal("0.001")


def test_position_size_stays_inside_the_per_trade_loss_budget():
    config = TradingConfig(
        risk_per_trade=Decimal("0.005"),
        stop_loss_pct=Decimal("0.0075"),
        max_position_pct=Decimal("1"),
        slippage_pct=Decimal("0"),
        spread_pct=Decimal("0"),
    )
    equity = Decimal("100000")
    decision = RiskManager(config, "US").size_buy(
        equity=equity,
        cash=equity,
        price=Decimal("100"),
        open_count=0,
        currency="USD",
        timestamp=START,
    )
    assert decision.allowed and decision.quantity is not None
    loss = decision.quantity * Decimal("100") * config.stop_loss_pct
    assert loss <= equity * config.risk_per_trade


def test_drawdown_halt_blocks_a_later_entry():
    config = _config(
        risk_per_trade=Decimal("0.5"),
        max_position_pct=Decimal("0.5"),
        max_drawdown_pct=Decimal("0.10"),
        daily_loss_pct=Decimal("0.50"),
        stop_loss_pct=Decimal("0.50"),
    )
    risk = RiskManager(config, "US")
    risk.update_limits("USD", Decimal("1000"), START)
    risk.update_limits("USD", Decimal("890"), START + timedelta(minutes=5))
    blocked = risk.size_buy(
        equity=Decimal("890"),
        cash=Decimal("890"),
        price=Decimal("100"),
        open_count=0,
        currency="USD",
        timestamp=START + timedelta(minutes=5),
    )
    assert risk.halted
    assert blocked.allowed is False
    assert blocked.reason == "MAX_DRAWDOWN"

    aaa = [
        _bar("AAA", 0, "100", open_="100", low="99"),
        _bar("AAA", 1, "80", open_="100", low="79"),
        _bar("AAA", 2, "80", open_="80", low="79"),
    ]
    bbb = [_bar("BBB", index, "100", open_="100", low="99") for index in range(3)]
    output = run_backtest({"AAA": aaa, "BBB": bbb}, {"AAA": "USD", "BBB": "USD"}, _CrashThenOther(), config, "US", {"USD": Decimal("1000")})
    assert any(event["code"] == "MAX_DRAWDOWN" for event in output.risk_events)
    assert {fill.symbol for fill in output.fills} == {"AAA"}


def test_missing_bars_are_not_filled_and_a_short_history_is_refused():
    with pytest.raises(InsufficientDataError):
        run_backtest({}, {}, BuyHoldStrategy(_config()), _config(), "US", {"USD": Decimal("1000")})
    with pytest.raises(InsufficientDataError):
        run_backtest({"AAPL": [_bar("AAPL", 0, "100")]}, {"AAPL": "USD"}, BuyHoldStrategy(_config()), _config(), "US", {"USD": Decimal("1000")})

    aaa = [_bar("AAA", 0, "100", open_="100"), _bar("AAA", 3, "100", open_="100")]
    bbb = [_bar("BBB", 0, "100", open_="100"), _bar("BBB", 1, "100", open_="100")]
    output = run_backtest(
        {"AAA": aaa, "BBB": bbb},
        {"AAA": "USD", "BBB": "USD"},
        _FirstBuy(),
        _config(),
        "US",
        {"USD": Decimal("10000")},
    )
    assert {fill.symbol for fill in output.fills} == {"BBB"}
    assert any(event["code"] == "DATA_GAP" and event["details"]["symbol"] == "AAA" for event in output.risk_events)


def test_jpy_and_usd_cash_are_not_converted_without_an_fx_rate():
    book = Portfolio({"JPY": Decimal("1000000"), "USD": Decimal("100")})
    book.apply_buy("AAPL", "USD", Decimal("1"), Decimal("50"), Decimal("0"), START, None, None)
    book.mark("AAPL", Decimal("50"))
    assert book.cash["JPY"] == Decimal("1000000")
    assert book.cash["USD"] == Decimal("50")
    assert book.reporting_equity("JPY", {}) is None
    assert book.reporting_equity("JPY", {("USD", "JPY"): Decimal("150")}) == Decimal("1000000") + Decimal("100") * Decimal("150")


def test_the_same_inputs_reproduce_the_same_backtest():
    bars = _series("AAPL", [Decimal(100), Decimal(101), Decimal(102), Decimal(103)])
    kwargs = dict(
        bars_by_symbol={"AAPL": bars},
        currencies={"AAPL": "USD"},
        config=_config(),
        market="US",
        initial_cash={"USD": Decimal("5000")},
    )
    first = run_backtest(strategy=BuyHoldStrategy(_config()), **kwargs)
    second = run_backtest(strategy=BuyHoldStrategy(_config()), **kwargs)
    assert first.metrics == second.metrics
    assert [(item.price, item.quantity, item.executed_at) for item in first.fills] == [
        (item.price, item.quantity, item.executed_at) for item in second.fills
    ]


def test_live_mode_is_still_refused():
    with pytest.raises(RuntimeError):
        assert_phase1_safety(Settings(live_trading_enabled=True))
    with pytest.raises(RuntimeError):
        assert_phase1_safety(Settings(trading_mode="LIVE"))
    assert not Path("app/brokers").exists()


def test_unknown_metrics_stay_null():
    metrics = compute_metrics([], [], Decimal("1000"))
    assert metrics["total_pnl"] is None
    assert metrics["win_rate"] is None
    assert metrics["profit_factor"] is None
    assert metrics["sharpe_ratio"] is None
    assert metrics["max_drawdown"] is None


def test_a_bar_that_hits_stop_and_target_fills_the_stop():
    config = _config(stop_loss_pct=Decimal("0.10"), take_profit_pct=Decimal("0.10"))
    portfolio = Portfolio({"USD": Decimal("10000")})
    broker = SimulatedBroker(portfolio, config)
    order = broker.submit_market(
        symbol="AAPL",
        side="BUY",
        quantity=Decimal("1"),
        currency="USD",
        execute_at=START,
        idempotency_key="entry",
        reserve_price=Decimal("100"),
        created_at=START,
    )
    broker.fill_market(order, _bar("AAPL", 0, "100", open_="100", high="100", low="100"))
    exit_bar = _bar("AAPL", 1, "100", open_="100", high="120", low="80")
    fill = broker.protective_exit("AAPL", exit_bar, exit_bar.timestamp)
    assert fill is not None
    assert fill.price == Decimal("90")
    assert "STOP_BEFORE_TARGET" in broker.orders[fill.order_id].events


def test_same_timestamp_buys_share_cash_instead_of_filling_the_first_symbol_only():
    bars = {
        "AAA": [_bar("AAA", 0, "100"), _bar("AAA", 1, "100")],
        "BBB": [_bar("BBB", 0, "100"), _bar("BBB", 1, "100")],
    }
    output = run_backtest(
        bars,
        {"AAA": "USD", "BBB": "USD"},
        BuyHoldStrategy(_config()),
        _config(),
        "US",
        {"USD": Decimal("1000")},
    )
    quantities = {fill.symbol: fill.quantity for fill in output.fills}
    assert quantities["AAA"] == quantities["BBB"]
    assert quantities["AAA"] > 0


@pytest.fixture(scope="session")
def database_ready():
    from sqlalchemy import text

    from app.db.session import engine

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - unit tests still run when PostgreSQL is down
        pytest.skip(f"PostgreSQL is not available: {exc}")
    return engine


def test_backtest_api_persists_a_run_and_refuses_missing_data(database_ready):
    from fastapi.testclient import TestClient
    from sqlalchemy import delete, select

    from app.data.bar import NormalizedBar
    from app.data.repository import save_bars
    from app.db.session import SessionLocal
    from app.main import app
    from app.models import Instrument, MarketBar
    from app.models.trading import BacktestRun

    rows = [
        NormalizedBar(
            market="US",
            symbol="ZZBT",
            timestamp=START + timedelta(minutes=5 * index),
            timeframe="5m",
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("1000"),
            provider="phase2",
        )
        for index in range(3)
    ]
    with SessionLocal() as session:
        save_bars(session, rows, now=datetime(2024, 1, 3, tzinfo=UTC))
    client = TestClient(app)
    run_id = None
    missing_id = None
    try:
        created = client.post(
            "/api/v1/backtests",
            json={
                "strategy": "buy_hold",
                "market": "US",
                "symbols": ["ZZBT"],
                "start": "2024-01-02T14:30:00Z",
                "end": "2024-01-02T15:00:00Z",
                "provider": "phase2",
                "initial_cash": {"USD": "10000"},
                "parameters": {"slippage_pct": "0", "spread_pct": "0", "stop_loss_pct": "0.5"},
            },
        )
        assert created.status_code == 202
        assert created.json()["status"] == "PENDING"
        run_id = created.json()["id"]
        detail = client.get(f"/api/v1/backtests/{run_id}")
        assert detail.status_code == 200
        body = detail.json()
        assert body["status"] == "SUCCEEDED"
        assert body["metrics"]["trade_count"] == 1
        assert body["metrics"]["win_rate"] is None
        trades = client.get(f"/api/v1/backtests/{run_id}/trades").json()
        assert len(trades) == 1
        assert trades[0]["signal_time"] != trades[0]["fill_time"]
        equity = client.get(f"/api/v1/backtests/{run_id}/equity").json()
        assert equity
        missing = client.post(
            "/api/v1/backtests",
            json={
                "strategy": "buy_hold",
                "market": "US",
                "symbols": ["ZZNONE"],
                "start": "2024-01-02T14:30:00Z",
                "end": "2024-01-02T15:00:00Z",
                "provider": "phase2",
                "initial_cash": {"USD": "10000"},
            },
        )
        missing_id = missing.json()["id"]
        failed = client.get(f"/api/v1/backtests/{missing_id}").json()
        assert failed["status"] == "FAILED"
        assert failed["metrics"] is None
        strategies = client.get("/api/v1/strategies")
        assert strategies.status_code == 200
        assert {item["name"] for item in strategies.json()} >= {"ema_cross", "rsi_reversion", "buy_hold"}
        portfolio = client.get("/api/v1/portfolio")
        assert portfolio.status_code == 200
        assert "JPY" in portfolio.json()["cash"]
        assert client.get("/api/v1/orders").status_code == 200
        assert client.get("/api/v1/risk/events").status_code == 200
        assert client.get("/health").status_code == 200
    finally:
        with SessionLocal() as session:
            ids = [item for item in (run_id, missing_id) if item is not None]
            if ids:
                session.execute(delete(BacktestRun).where(BacktestRun.id.in_(ids)))
            instrument = session.scalar(select(Instrument).where(Instrument.symbol == "ZZBT"))
            if instrument is not None:
                session.execute(delete(MarketBar).where(MarketBar.instrument_id == instrument.id))
                session.delete(instrument)
            session.commit()
