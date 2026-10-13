from __future__ import annotations

import time

from sqlalchemy import text

from app.db.session import engine


def wait_for_db(attempts: int = 30, delay_seconds: float = 1.0) -> None:
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return
        except Exception as exc:  # noqa: BLE001 - retry until PostgreSQL accepts connections
            last_error = exc
            time.sleep(delay_seconds)
    raise RuntimeError(f"PostgreSQL did not become ready: {last_error}")


if __name__ == "__main__":
    wait_for_db()
