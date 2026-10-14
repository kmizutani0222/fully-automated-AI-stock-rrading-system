from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from app.db.guard import assert_pytest_database, database_name

ROOT = Path(__file__).resolve().parents[1]
OPERATIONAL_DATABASE_URL = os.environ.get("DATABASE_URL", "")
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

try:
    SELECTED_TEST_DATABASE_URL = assert_pytest_database(OPERATIONAL_DATABASE_URL, TEST_DATABASE_URL)
except RuntimeError as exc:
    raise SystemExit(str(exc)) from exc

os.environ["DATABASE_URL"] = SELECTED_TEST_DATABASE_URL


def pytest_sessionstart(session) -> None:
    """Create and migrate the test database before any test imports the application engine."""
    del session
    _ensure_test_database()
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=os.environ.copy(),
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit("pytest refused to start because migrating the test database failed.")
    from app.core.config import get_settings
    from app.db.session import engine

    selected = database_name(str(engine.url))
    expected = database_name(SELECTED_TEST_DATABASE_URL)
    operational = database_name(OPERATIONAL_DATABASE_URL)
    configured = database_name(get_settings().database_url)
    if selected != expected or configured != expected or selected == operational:
        raise SystemExit(
            f"pytest refused to start because the application engine is on '{selected}', not the test database."
        )


def _ensure_test_database() -> None:
    from sqlalchemy import create_engine, text

    name = database_name(SELECTED_TEST_DATABASE_URL)
    if not name.replace("_", "").isalnum():
        raise SystemExit("pytest refused to start because the test database name is not a plain identifier.")
    admin = create_engine(OPERATIONAL_DATABASE_URL, isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    try:
        with admin.connect() as connection:
            current = connection.execute(text("SELECT current_database()")).scalar()
            if current == name:
                raise SystemExit("pytest refused to create a database from a connection that is already the test target.")
            exists = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": name},
            ).scalar()
            if exists is None:
                connection.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        admin.dispose()
