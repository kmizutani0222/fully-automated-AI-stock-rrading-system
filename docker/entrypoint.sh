#!/bin/sh
set -eu
python -m app.db.wait_for_db
alembic upgrade head
python scripts/seed_instruments.py
exec "$@"
