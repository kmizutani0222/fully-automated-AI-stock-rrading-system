#!/usr/bin/env python
"""Insert the instrument catalog. Existing symbols are left in place."""

from app.db.seed import seed_instruments
from app.db.session import SessionLocal


def main() -> None:
    with SessionLocal() as session:
        count = seed_instruments(session)
    print(f"Instrument catalog rows: {count}")


if __name__ == "__main__":
    main()
