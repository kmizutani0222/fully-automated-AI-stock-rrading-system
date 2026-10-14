#!/bin/sh
set -eu
python - <<'PY'
import os
import sys

from app.db.guard import assert_runtime_database

try:
    assert_runtime_database(os.environ.get("DATABASE_URL", ""))
except RuntimeError as exc:
    print(exc, file=sys.stderr)
    raise SystemExit(1) from exc
PY
python -m app.db.wait_for_db
alembic upgrade head
python scripts/seed_instruments.py
exec "$@"
