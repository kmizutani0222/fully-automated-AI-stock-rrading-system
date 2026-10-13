from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.ai.calendar import session_bar_starts
from app.ai.config import load_ai_config
from app.ai.features import compute_features
from app.ai.labels import DAY_COLUMNS
from app.ai.ranking import RankedSymbol, rank_symbols
from app.ai.regimes import REGIMES, RuleRegimeClassifier, build_regime_classifier
from app.ai.registry import ModelRecord, ModelRegistry
from app.ai.selector import Forecast, select_strategy
from app.ai.splits import purge_and_embargo, walk_forward
from app.ai.strategy import ReplayStrategy
from app.ai.training import fit_booster, load_boosters, save_boosters, train_samples
from app.backtest.engine import run_backtest
from app.core.config import Settings, assert_phase1_safety
from app.risk.manager import RiskManager
from app.trading.config import TradingConfig
from app.trading.indicators import ema
from app.trading.strategies import PriceBar

UTC = timezone.utc
NOW = datetime(2024, 6, 1, tzinfo=UTC)


def _loose(**overrides):
    values = {
        "min_samples": 30,
        "min_calendar_days": 3,
        "min_regimes": 1,
        "min_folds": 2,
        "min_test_samples": 4,
        "embargo_minutes": 0,
    }
    values.update(overrides)
    return load_ai_config().with_overrides(values)


def _price_bar(symbol: str, timestamp: datetime, close: str, volume: str = "1000") -> PriceBar:
    price = Decimal(close)
    return PriceBar(symbol, timestamp, price, price + Decimal("0.2"), price - Decimal("0.2"), price, Decimal(volume))


def _session(day: date, symbol: str, seed: int) -> list[PriceBar]:
    bars = []
    for index, timestamp in enumerate(session_bar_starts("US", day)):
        value = 100 + seed + math.sin(index / 4) * 2
        bars.append(_price_bar(symbol, timestamp, f"{value:.4f}"))
    return bars


def _sessions(symbol: str, count: int, start: date = date(2024, 1, 2)) -> list[PriceBar]:
    bars: list[PriceBar] = []
    day = start
    made = 0
    while made < count:
        chunk = _session(day, symbol, made)
        if chunk:
            bars.extend(chunk)
            made += 1
        day += timedelta(days=1)
    return bars


def _forecast(symbol: str, style: str, expected: float, steps: int, *, expired: bool = False, insample: bool = False, probability=0.7, risk=0.01, gap=0.001) -> Forecast:
    start = datetime(2024, 1, 2, 15, 0, tzinfo=UTC)
    return Forecast(
        symbol=symbol,
        style=style,
        horizon_steps=steps,
        expected_return=expected,
        up_probability=probability,
        risk=risk,
        gap_risk=gap,
        available_at=start,
        expires_at=start if expired else start + timedelta(hours=2),
        is_out_of_sample=not insample,
    )


def test_feature_values_match_hand_calculation():
    start = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    closes = [Decimal(value) for value in range(10, 31)]
    bars = [_price_bar("AAPL", start + timedelta(minutes=5 * index), str(close)) for index, close in enumerate(closes)]
    rows = compute_features(bars, "US", now=NOW)
    assert rows[1].values["return_1"] == pytest.approx(11 / 10 - 1)
    assert rows[19].values["sma_20"] == pytest.approx(sum(range(10, 30)) / 20)
    expected_ema = ema(closes, 9)
    assert rows[12].values["ema_9"] == pytest.approx(float(expected_ema[12]))
    assert rows[1].values["tf15_return"] is None
    assert rows[5].values["tf15_return"] == pytest.approx(float(closes[5] / closes[2] - 1))
    assert rows[0].available_at == bars[0].timestamp + timedelta(minutes=5)


def test_features_do_not_change_when_future_bars_change():
    bars = _sessions("AAPL", 1)
    original = compute_features(bars, "US", now=NOW)
    changed = list(bars)
    last = changed[-1]
    changed[-1] = PriceBar(last.symbol, last.timestamp, Decimal("999"), Decimal("1000"), Decimal("998"), Decimal("999"), last.volume)
    mutated = compute_features(changed, "US", now=NOW)
    assert original[10].values == mutated[10].values
    prefix = compute_features(bars[:11], "US", now=NOW)
    assert prefix[10].values["return_1"] == original[10].values["return_1"]
    assert prefix[10].values["sma_20"] == original[10].values["sma_20"]


def test_open_bars_are_excluded_from_features():
    closed = _price_bar("AAPL", datetime(2024, 1, 2, 14, 30, tzinfo=UTC), "10")
    fresh = _price_bar("AAPL", datetime.now(UTC), "11")
    rows = compute_features([closed, fresh], "US")
    assert [row.timestamp for row in rows] == [closed.timestamp]


def test_time_split_is_ordered_and_disjoint():
    start = datetime(2024, 1, 2, tzinfo=UTC)
    times = [start + timedelta(minutes=5 * index) for index in range(80)]
    ends = [item + timedelta(minutes=15) for item in times]
    folds = walk_forward(times, ends, n_folds=2, embargo=timedelta(0))
    assert len(folds) == 2
    for fold in folds:
        assert set(fold.train).isdisjoint(fold.validation)
        assert set(fold.train).isdisjoint(fold.test)
        assert set(fold.validation).isdisjoint(fold.test)
        assert max(fold.train) < min(fold.validation) < max(fold.validation) < min(fold.test)


def test_purge_and_embargo_remove_overlapping_labels():
    start = datetime(2024, 1, 2, tzinfo=UTC)
    times = [start + timedelta(minutes=5 * index) for index in range(30)]
    ends = [item + timedelta(minutes=30) for item in times]
    train = tuple(range(10))
    validation = tuple(range(10, 20))
    test = tuple(range(20, 30))
    purged_train, purged_validation = purge_and_embargo(times, ends, train, validation, test, timedelta(minutes=10))
    assert all(ends[index] <= times[validation[0]] for index in purged_train)
    assert all(times[index] < times[validation[0]] - timedelta(minutes=10) for index in purged_train)
    assert 9 not in purged_train
    assert all(ends[index] <= times[test[0]] for index in purged_validation)


def test_short_history_refuses_training(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("learner must not run")

    monkeypatch.setattr("app.ai.training.fit_booster", fail_if_called)
    samples = []
    start = datetime(2026, 10, 12, 15, 30, tzinfo=UTC)
    for index in range(77):
        timestamp = start + timedelta(minutes=5 * index)
        samples.append(
            {
                "symbol": "AAPL",
                "timestamp": timestamp,
                "available_at": timestamp + timedelta(minutes=5),
                "label_end": timestamp + timedelta(minutes=60),
                "features": {name: 0.01 for name in DAY_COLUMNS},
                "labels": {"return_15m": 0.01},
            }
        )
    output = train_samples(samples, task="day", config=load_ai_config(), calendar_days=2, regime_names=["UNCERTAIN"])
    assert output.status == "REFUSED"
    assert output.models == {}
    assert "samples" in output.reason
    assert "calendar days" in output.reason


def test_learner_failure_does_not_activate(monkeypatch):
    monkeypatch.setattr("app.ai.training.fit_booster", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    samples = _labeled_samples(120)
    output = train_samples(samples, task="day", config=_loose(), calendar_days=5, regime_names=["RANGE", "TREND_UP"])
    assert output.status == "FAILED"
    assert output.reason == "boom"
    registry = ModelRegistry()
    registry.add(ModelRecord("bad", "day", "FAILED", {"return_15m": {"test_rmse": 0.1, "test_samples": 5}}, "path"))
    with pytest.raises(ValueError, match="failed"):
        registry.activate("bad")


def test_model_round_trip_save_and_load(tmp_path: Path):
    model = fit_booster(([[1.0], [2.0], [3.0], [4.0]], [0.1, 0.2, 0.1, 0.2]), {"num_leaves": 7}, _loose(), objective="regression")
    save_boosters(tmp_path, {"return_15m": {"return": model, "up": None}})
    loaded = load_boosters(tmp_path, ["return_15m"])
    assert loaded["return_15m"]["return"].predict([[2.0]])[0] == pytest.approx(model.predict([[2.0]])[0])


def test_model_versions_and_activation_rules():
    registry = ModelRegistry()
    metrics = {"return_15m": {"test_rmse": 0.2, "test_samples": 8}}
    registry.add(ModelRecord("v1", "day", "TRAINED", metrics, "data/models/v1"))
    registry.add(ModelRecord("v2", "day", "TRAINED", metrics, "data/models/v2"))
    registry.add(ModelRecord("missing", "day", "TRAINED", None, "data/models/missing"))
    registry.activate("v1")
    assert registry.records["v1"].status == "ACTIVE"
    registry.activate("v2")
    assert registry.records["v2"].status == "ACTIVE"
    assert registry.records["v1"].status == "INACTIVE"
    with pytest.raises(ValueError, match="test metrics"):
        registry.activate("missing")


def test_day_model_emits_finite_oos_predictions():
    from app.ai.features import compute_features
    from app.ai.labels import day_samples

    bars = _sessions("AAPL", 4)
    rows = compute_features(bars, "US", now=NOW)
    samples = day_samples(rows, bars, "US", (3, 6, 12))
    output = train_samples(samples, task="day", config=_loose(), calendar_days=4, regime_names=["RANGE", "UNCERTAIN"])
    assert output.status == "SUCCEEDED", output.reason
    assert output.predictions
    assert output.train_end < output.test_start
    assert all(item.is_out_of_sample for item in output.predictions)
    assert all(item.timestamp >= output.test_start for item in output.predictions)
    assert all(math.isfinite(item.expected_return) for item in output.predictions)
    assert all(item < output.test_start for item in output.tuning_timestamps)


def test_swing_model_emits_finite_oos_predictions_and_gap_risk():
    from app.ai.features import completed_daily_bars
    from app.ai.labels import swing_samples

    stock = _sessions("AAPL", 45)
    index = _sessions("SPY", 45)
    samples = swing_samples(completed_daily_bars(stock, "US", now=NOW), completed_daily_bars(index, "US", now=NOW), "US", (1, 3, 5))
    output = train_samples(
        samples,
        task="swing",
        config=_loose(min_samples=8, min_calendar_days=20, min_test_samples=2),
        calendar_days=45,
        regime_names=["RANGE"],
    )
    assert output.status == "SUCCEEDED", output.reason
    assert output.predictions
    assert all(item.gap_risk is not None and item.gap_risk >= 0 for item in output.predictions)
    assert all(item.timestamp >= output.test_start for item in output.predictions)


def test_rule_regime_labels():
    classifier = RuleRegimeClassifier(load_ai_config())
    base = {"ema_9": 12, "ema_21": 11, "ema_50": 10, "adx_14": 30, "atr_14": 0.01, "return_12": 0.01, "realized_vol_12": 0.001}
    assert classifier.classify(base) == "TREND_UP"
    assert classifier.classify({**base, "ema_9": 9, "ema_21": 10, "ema_50": 11, "return_12": -0.02}) == "TREND_DOWN"
    assert classifier.classify({**base, "adx_14": 10}) == "RANGE"
    assert classifier.classify({**base, "atr_14": 1}) == "HIGH_VOL"
    assert classifier.classify({**base, "adx_14": 22, "ema_9": 10}) == "UNCERTAIN"
    assert set(REGIMES) == {"TREND_UP", "TREND_DOWN", "RANGE", "HIGH_VOL", "UNCERTAIN"}
    with pytest.raises(NotImplementedError):
        build_regime_classifier("lightgbm", load_ai_config()).classify(base)


def test_selector_chooses_day():
    now = datetime(2024, 1, 2, 15, 0, tzinfo=UTC)
    decision = select_strategy("AAPL", [_forecast("AAPL", "DAY", 0.02, 3)], [_forecast("AAPL", "SWING", 0.01, 5)], cost=0.001, now=now)
    assert decision.choice == "DAY"
    assert decision.score > 0


def test_selector_chooses_swing_without_transferring_a_day_position():
    now = datetime(2024, 1, 2, 15, 0, tzinfo=UTC)
    decision = select_strategy("AAPL", [_forecast("AAPL", "DAY", 0.01, 12)], [_forecast("AAPL", "SWING", 0.05, 1)], cost=0.001, now=now)
    assert decision.choice == "SWING"
    held = select_strategy("AAPL", [_forecast("AAPL", "DAY", 0.01, 3)], [_forecast("AAPL", "SWING", 0.05, 1)], cost=0.001, now=now, open_style="DAY")
    assert held.choice == "CASH"
    assert held.managed_styles["AAPL"] == "DAY"


def test_selector_chooses_cash_when_the_prediction_is_missing():
    decision = select_strategy("AAPL", [], [], cost=0.001, now=datetime(2024, 1, 2, tzinfo=UTC))
    assert decision.choice == "CASH"
    assert decision.reason == "prediction_missing"


def test_costs_can_turn_a_positive_return_into_cash():
    now = datetime(2024, 1, 2, 15, 0, tzinfo=UTC)
    decision = select_strategy("AAPL", [_forecast("AAPL", "DAY", 0.001, 3)], [], cost=0.002, now=now)
    assert decision.choice == "CASH"
    assert decision.reason == "cost_exceeds_expected_return"


def test_selector_signal_still_passes_through_risk_manager():
    now = datetime(2024, 1, 2, 15, 0, tzinfo=UTC)
    decision = select_strategy("AAPL", [_forecast("AAPL", "DAY", 0.02, 3)], [], cost=0.0001, now=now)
    assert decision.choice == "DAY"
    risk = RiskManager(TradingConfig(), "US")
    risk.halted = True
    risk.halt_reason = "MAX_DRAWDOWN"
    blocked = risk.size_buy(equity=Decimal("10000"), cash=Decimal("10000"), price=Decimal("100"), open_count=0, currency="USD", timestamp=now)
    assert blocked.allowed is False
    bars = [_price_bar("AAPL", now + timedelta(minutes=5 * index), "100") for index in range(3)]
    output = run_backtest(
        {"AAPL": bars},
        {"AAPL": "USD"},
        ReplayStrategy({("AAPL", bars[0].timestamp): decision}),
        TradingConfig(slippage_pct=Decimal("0"), spread_pct=Decimal("0"), stop_loss_pct=Decimal("0.5"), risk_per_trade=Decimal("1"), max_position_pct=Decimal("1"), us_lot=Decimal("1")),
        "US",
        {"USD": Decimal("50")},
    )
    assert output.fills == []
    assert any(event["code"] == "INSUFFICIENT_CASH" for event in output.risk_events)


def test_symbols_are_ranked_by_score_not_name():
    ranked = rank_symbols(
        [
            RankedSymbol("AAA", net_return=0.01, risk=0.02, liquidity=1000, correlation=0.1),
            RankedSymbol("MMM", net_return=0.03, risk=0.01, liquidity=1000, correlation=0.1),
            RankedSymbol("ZZZ", net_return=0.03, risk=0.01, liquidity=1000, correlation=0.9),
        ]
    )
    assert [item.symbol for item in ranked] == ["MMM", "AAA", "ZZZ"]


def test_missing_prediction_does_not_order():
    decision = select_strategy("AAPL", [], [], cost=0.001, now=datetime(2024, 1, 2, tzinfo=UTC))
    strategy = ReplayStrategy({})
    bars = [_price_bar("AAPL", datetime(2024, 1, 2, 14, 30, tzinfo=UTC), "100"), _price_bar("AAPL", datetime(2024, 1, 2, 14, 35, tzinfo=UTC), "100")]
    assert decision.choice == "CASH"
    assert strategy.evaluate(bars) is None


def test_expired_prediction_is_rejected():
    now = datetime(2024, 1, 2, 15, 0, tzinfo=UTC)
    decision = select_strategy("AAPL", [_forecast("AAPL", "DAY", 0.05, 3, expired=True)], [], cost=0.0001, now=now)
    assert decision.choice == "CASH"
    assert decision.reason == "prediction_expired"


def test_ai_backtest_is_reproducible():
    now = datetime(2024, 1, 2, 15, 0, tzinfo=UTC)
    decision = select_strategy("AAPL", [_forecast("AAPL", "DAY", 0.02, 3)], [], cost=0.0001, now=now)
    bars = [_price_bar("AAPL", now + timedelta(minutes=5 * index), "100") for index in range(4)]
    strategy_args = {
        "bars_by_symbol": {"AAPL": bars},
        "currencies": {"AAPL": "USD"},
        "config": TradingConfig(slippage_pct=Decimal("0"), spread_pct=Decimal("0"), stop_loss_pct=Decimal("0.5")),
        "market": "US",
        "initial_cash": {"USD": Decimal("10000")},
    }
    first = run_backtest(strategy=ReplayStrategy({( "AAPL", bars[0].timestamp): decision}), **strategy_args)
    second = run_backtest(strategy=ReplayStrategy({("AAPL", bars[0].timestamp): decision}), **strategy_args)
    assert [(item.price, item.quantity, item.executed_at) for item in first.fills] == [
        (item.price, item.quantity, item.executed_at) for item in second.fills
    ]
    assert first.metrics == second.metrics


def test_insample_predictions_are_not_traded():
    now = datetime(2024, 1, 2, 15, 0, tzinfo=UTC)
    decision = select_strategy("AAPL", [_forecast("AAPL", "DAY", 0.2, 3, insample=True)], [], cost=0.0001, now=now)
    assert decision.choice == "CASH"
    assert decision.reason == "prediction_insample"
    bars = [_price_bar("AAPL", now, "100"), _price_bar("AAPL", now + timedelta(minutes=5), "100")]
    output = run_backtest(
        {"AAPL": bars},
        {"AAPL": "USD"},
        ReplayStrategy({}),
        TradingConfig(),
        "US",
        {"USD": Decimal("10000")},
    )
    assert output.fills == []


def test_live_mode_is_still_refused():
    with pytest.raises(RuntimeError):
        assert_phase1_safety(Settings(live_trading_enabled=True))
    with pytest.raises(RuntimeError):
        assert_phase1_safety(Settings(trading_mode="LIVE"))
    assert not Path("app/brokers").exists()
    assert "SimulatedBroker" not in Path("app/ai/selector.py").read_text(encoding="utf-8")
    assert "submit_market" not in Path("app/ai/strategy.py").read_text(encoding="utf-8")


def test_ai_api_refuses_the_short_aapl_history(database_ready):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.models.ai import ModelVersion

    client = TestClient(app)
    created = client.post(
        "/api/v1/ai/train",
        json={
            "task": "day",
            "market": "US",
            "symbols": ["AAPL"],
            "start": "2026-10-12T00:00:00Z",
            "end": "2026-10-14T00:00:00Z",
            "provider": "yfinance",
        },
    )
    assert created.status_code == 202
    body = client.get(f"/api/v1/ai/training-runs/{created.json()['id']}").json()
    assert body["status"] == "REFUSED"
    assert body["model_version_id"] is None
    assert body["reason"]
    assert client.get("/api/v1/ai/models").status_code == 200
    assert client.get("/api/v1/ai/predictions").status_code == 200
    assert client.get("/api/v1/ai/regimes").status_code == 200
    assert client.get("/api/v1/ai/decisions").status_code == 200
    assert client.post("/api/v1/ai/models/999999/activate").status_code == 404
    compared = client.post(
        "/api/v1/ai/backtests",
        json={
            "market": "US",
            "symbols": ["AAPL"],
            "start": "2026-10-12T00:00:00Z",
            "end": "2026-10-14T00:00:00Z",
            "provider": "yfinance",
            "initial_cash": {"USD": "10000"},
        },
    )
    assert compared.status_code == 202
    ai_runs = [item for item in compared.json()["runs"] if item["strategy"] in {"day_ai", "swing_ai", "ai_selector"}]
    for item in ai_runs:
        detail = client.get(f"/api/v1/backtests/{item['id']}").json()
        assert detail["status"] == "FAILED"
        assert detail["metrics"] is None
        assert "out-of-sample" in detail["error"]
    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/strategies").status_code == 200


def _labeled_samples(count: int) -> list[dict]:
    start = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    samples = []
    for index in range(count):
        timestamp = start + timedelta(minutes=5 * index)
        samples.append(
            {
                "symbol": "AAPL",
                "timestamp": timestamp,
                "available_at": timestamp + timedelta(minutes=5),
                "label_end": timestamp + timedelta(minutes=15),
                "label_ends": {"return_15m": timestamp + timedelta(minutes=15)},
                "features": {name: math.sin(index) for name in DAY_COLUMNS},
                "labels": {"return_15m": 0.01 if index % 2 == 0 else -0.01},
            }
        )
    return samples


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
