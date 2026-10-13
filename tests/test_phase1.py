from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import delete, func, select, text

from app.core.config import Settings, assert_phase1_safety
from app.core.constants import TIMEFRAME
from app.data.bar import NormalizedBar
from app.data.csv_loader import load_csv
from app.data.normalizer import parse_timestamp
from app.data.providers.factory import build_provider
from app.data.providers.mock import MockProvider
from app.data.providers.yfinance_provider import frame_from_yfinance
from app.data.quality import drop_open_bars, inspect_bars
from app.data.validator import validate_bar

UTC = timezone.utc


def _bar(**overrides) -> NormalizedBar:
    values = {
        "market": "US",
        "symbol": "AAPL",
        "timestamp": datetime(2024, 1, 2, 14, 30, tzinfo=UTC),
        "timeframe": TIMEFRAME,
        "open": Decimal("100"),
        "high": Decimal("101"),
        "low": Decimal("99"),
        "close": Decimal("100.5"),
        "volume": Decimal("1000"),
        "provider": "csv",
    }
    values.update(overrides)
    return NormalizedBar(**values)


def test_market_bar_validation_accepts_a_regular_bar():
    assert validate_bar(_bar()) == []


def test_market_bar_validation_rejects_bad_ohlc_volume_and_alignment():
    assert "high must be greater than or equal to low" in validate_bar(_bar(high=Decimal("90"), low=Decimal("99")))
    assert "low must be less than or equal to close" in validate_bar(_bar(low=Decimal("100.8")))
    assert "volume must be zero or positive" in validate_bar(_bar(volume=Decimal("-1")))
    assert "open must be positive" in validate_bar(_bar(open=Decimal("0")))
    assert any("not aligned" in reason for reason in validate_bar(_bar(timestamp=datetime(2024, 1, 2, 14, 31, tzinfo=UTC))))
    assert "timeframe must be 5m" in validate_bar(_bar(timeframe="1m"))


def test_csv_normalizes_jst_and_skips_invalid_rows(tmp_path: Path):
    path = tmp_path / "bars.csv"
    path.write_text(
        "\n".join(
            [
                "market,symbol,timestamp,open,high,low,close,volume",
                "JP,7203,2024-01-04 09:00:00,2500,2510,2490,2505,1000",
                "JP,7203,2024-01-04 09:05:00,10,9,8,9,1000",
                "US,AAPL,2024-01-02T14:31:00Z,100,101,99,100,10",
            ]
        ),
        encoding="utf-8",
    )
    loaded = load_csv(path, "Asia/Tokyo")
    assert len(loaded.bars) == 3
    assert loaded.bars[0].timestamp == datetime(2024, 1, 4, 0, 0, tzinfo=UTC)
    assert loaded.bars[0].symbol == "7203"
    report = inspect_bars(loaded.bars)
    assert report.valid_rows == 1
    assert len(report.rejected) == 2


def test_quality_flags_intraday_gap_but_not_japan_lunch_or_overnight():
    morning = _bar(market="JP", symbol="7203", timestamp=parse_timestamp("2024-01-04 11:30:00", "Asia/Tokyo"), provider="csv")
    afternoon = _bar(market="JP", symbol="7203", timestamp=parse_timestamp("2024-01-04 12:30:00", "Asia/Tokyo"), provider="csv")
    assert inspect_bars([morning, afternoon]).gaps == []

    first = _bar(timestamp=datetime(2024, 1, 2, 14, 30, tzinfo=UTC))
    missing = _bar(timestamp=datetime(2024, 1, 2, 14, 45, tzinfo=UTC))
    gaps = inspect_bars([first, missing]).gaps
    assert len(gaps) == 1
    assert gaps[0].minutes == 15

    next_day = _bar(timestamp=datetime(2024, 1, 3, 14, 30, tzinfo=UTC))
    assert inspect_bars([first, next_day]).gaps == []


def test_duplicate_keys_are_reported_and_not_filled():
    report = inspect_bars([_bar(), _bar(close=Decimal("101"))])
    assert len(report.duplicate_keys) == 1
    assert report.valid_rows == 1
    assert report.gaps == []


def test_open_bar_is_not_treated_as_closed():
    future = datetime.now(UTC).replace(second=0, microsecond=0) + timedelta(minutes=10)
    future = future.replace(minute=future.minute - (future.minute % 5))
    closed, dropped = drop_open_bars([_bar(timestamp=future)])
    assert closed == []
    assert dropped == 1


@pytest.mark.asyncio
async def test_mock_provider_returns_valid_five_minute_bars():
    provider = MockProvider()
    start = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    bars = await provider.get_bars("aapl", "5m", start, start + timedelta(minutes=20))
    assert len(bars) == 4
    assert all(validate_bar(bar) == [] for bar in bars)
    assert bars[0].provider == "mock"
    with pytest.raises(ValueError):
        await provider.get_bars("AAPL", "1m", start, start + timedelta(minutes=5))


def test_yfinance_frame_flattens_columns_and_converts_to_utc():
    index = pd.DatetimeIndex([pd.Timestamp("2024-01-02 09:30", tz="America/New_York")])
    columns = pd.MultiIndex.from_tuples(
        [("Open", "AAPL"), ("High", "AAPL"), ("Low", "AAPL"), ("Close", "AAPL"), ("Volume", "AAPL")]
    )
    frame = pd.DataFrame([[185.1, 185.4, 184.9, 185.2, 1200]], index=index, columns=columns)
    bars = frame_from_yfinance(frame, "AAPL")
    assert len(bars) == 1
    assert bars[0].timestamp == datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    assert bars[0].close == Decimal("185.20000000")
    assert bars[0].provider == "yfinance"
    assert validate_bar(bars[0]) == []


def test_provider_factory_uses_mock_only_when_asked():
    assert build_provider(Settings(market_data_provider="yfinance")).name == "yfinance"
    assert build_provider(Settings(market_data_provider="mock")).name == "mock"


def test_live_trading_is_refused_and_no_live_broker_exists():
    with pytest.raises(RuntimeError):
        assert_phase1_safety(Settings(live_trading_enabled=True))
    with pytest.raises(RuntimeError):
        assert_phase1_safety(Settings(trading_mode="LIVE"))
    assert not Path("app/brokers").exists()


@pytest.fixture(scope="session")
def database_ready():
    from app.db.session import engine

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - tests still run the units that do not need PostgreSQL
        pytest.skip(f"PostgreSQL is not available: {exc}")
    return engine


def test_csv_import_is_idempotent_and_skips_invalid_rows(database_ready, tmp_path: Path):
    from app.data.repository import get_instrument, save_bars
    from app.db.session import SessionLocal
    from app.models import Instrument, MarketBar

    path = tmp_path / "bars.csv"
    path.write_text(
        "\n".join(
            [
                "market,symbol,timestamp,open,high,low,close,volume",
                "US,ZZTEST,2024-01-02T14:30:00Z,100,101,99,100.5,10",
                "US,ZZTEST,2024-01-02T14:35:00Z,100.5,102,100,101,12",
                "US,ZZTEST,2024-01-02T14:40:00Z,10,9,8,9,1",
            ]
        ),
        encoding="utf-8",
    )
    loaded = load_csv(path)
    with SessionLocal() as session:
        try:
            first = save_bars(session, loaded.bars)
            assert first.saved == 2
            assert len(first.rejected) == 1
            revised = load_csv(path)
            revised.bars[0] = revised.bars[0].model_copy(update={"close": Decimal("100.8")})
            second = save_bars(session, revised.bars)
            assert second.saved == 2
            instrument = get_instrument(session, "US", "ZZTEST")
            assert instrument is not None
            count = session.scalar(select(func.count()).select_from(MarketBar).where(MarketBar.instrument_id == instrument.id))
            assert count == 2
            stored = session.scalar(
                select(MarketBar).where(MarketBar.instrument_id == instrument.id, MarketBar.timestamp == datetime(2024, 1, 2, 14, 30, tzinfo=UTC))
            )
            assert stored is not None
            assert stored.close == Decimal("100.80000000")
        finally:
            instrument = get_instrument(session, "US", "ZZTEST")
            if instrument is not None:
                session.execute(delete(MarketBar).where(MarketBar.instrument_id == instrument.id))
                session.execute(delete(Instrument).where(Instrument.id == instrument.id))
                session.commit()


def test_health_and_seeded_instruments(database_ready):
    from fastapi.testclient import TestClient

    from app.db.seed import seed_instruments
    from app.db.session import SessionLocal
    from app.main import app

    with SessionLocal() as session:
        seed_instruments(session)
    client = TestClient(app)
    health = client.get("/health")
    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["trading_mode"] == "PAPER"
    assert body["live_trading_enabled"] is False
    symbols = {row["symbol"] for row in client.get("/instruments").json()}
    assert {"AAPL", "MSFT", "NVDA", "SPY", "QQQ"} <= symbols
    missing = client.get("/bars/ZZMISSING")
    assert missing.status_code == 404


def test_health_returns_503_when_database_is_down(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    class BrokenEngine:
        def connect(self):
            raise OSError("connection refused")

    monkeypatch.setattr("app.api.health.engine", BrokenEngine())
    response = TestClient(app).get("/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["database"] != "ok"
    assert body["live_trading_enabled"] is False


def _run_import(path: Path, database_url: str | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if database_url is not None:
        env["DATABASE_URL"] = database_url
    return subprocess.run(
        [sys.executable, "scripts/import_csv.py", str(path)],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def test_import_command_is_idempotent_and_reports_gaps_and_bad_ohlc(database_ready, tmp_path: Path):
    from sqlalchemy import func, select

    from app.data.repository import get_instrument
    from app.db.session import SessionLocal
    from app.models import Instrument, MarketBar

    clean = tmp_path / "clean.csv"
    clean.write_text(
        "\n".join(
            [
                "market,symbol,timestamp,open,high,low,close,volume",
                "US,ZZCLI,2024-02-01T14:30:00Z,100,101,99,100.5,10",
                "US,ZZCLI,2024-02-01T14:35:00Z,100.5,102,100,101,11",
            ]
        ),
        encoding="utf-8",
    )
    first = _run_import(clean)
    second = _run_import(clean)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert "saved=2" in first.stdout
    assert "saved=2" in second.stdout

    broken = tmp_path / "broken.csv"
    broken.write_text(
        "\n".join(
            [
                "market,symbol,timestamp,open,high,low,close,volume",
                "US,ZZCLI,2024-02-01T15:30:00Z,100,101,99,100.5,10",
                "US,ZZCLI,2024-02-01T15:45:00Z,100,101,99,100.5,10",
                "US,ZZCLI,2024-02-01T15:50:00Z,10,9,8,9,1",
            ]
        ),
        encoding="utf-8",
    )
    reported = _run_import(broken)
    assert reported.returncode == 1
    assert "GAP ZZCLI" in reported.stdout
    assert "REJECTED ZZCLI" in reported.stdout
    assert "high must be greater than or equal to open" in reported.stdout

    with SessionLocal() as session:
        try:
            instrument = get_instrument(session, "US", "ZZCLI")
            assert instrument is not None
            count = session.scalar(select(func.count()).select_from(MarketBar).where(MarketBar.instrument_id == instrument.id))
            assert count == 4
        finally:
            instrument = get_instrument(session, "US", "ZZCLI")
            if instrument is not None:
                session.execute(delete(MarketBar).where(MarketBar.instrument_id == instrument.id))
                session.execute(delete(Instrument).where(Instrument.id == instrument.id))
                session.commit()


def test_import_command_fails_when_database_is_unreachable(tmp_path: Path):
    path = tmp_path / "bars.csv"
    path.write_text(
        "\n".join(
            [
                "market,symbol,timestamp,open,high,low,close,volume",
                "US,ZZDOWN,2024-02-01T14:30:00Z,100,101,99,100.5,10",
            ]
        ),
        encoding="utf-8",
    )
    completed = _run_import(path, "postgresql+psycopg://asts:asts@127.0.0.1:1/asts")
    assert completed.returncode != 0
    assert "saved=" not in completed.stdout
