#!/usr/bin/env python
"""Import a 5-minute CSV through the normalizer, validator, and repository."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.core.config import get_settings
from app.data.csv_loader import load_csv
from app.data.repository import save_bars
from app.db.session import SessionLocal


def main() -> None:
    parser = argparse.ArgumentParser(description="Import 5-minute bars from CSV")
    parser.add_argument("csv", type=Path)
    parser.add_argument("--timezone", default=None, help="Timezone used when timestamps have no offset")
    args = parser.parse_args()
    settings = get_settings()
    loaded = load_csv(args.csv, args.timezone or settings.csv_timezone)
    with SessionLocal() as session:
        result = save_bars(session, loaded.bars)
    print(
        f"saved={result.saved} rejected={len(result.rejected)} "
        f"gaps={len(result.quality.gaps)} parse_errors={len(loaded.errors)} "
        f"dropped_open_bars={result.quality.dropped_open_bars}"
    )
    for error in loaded.errors:
        print(f"PARSE_ERROR {error}")
    for item in result.rejected:
        print(f"REJECTED {item.symbol} {item.timestamp}: {', '.join(item.reasons)}")
    for gap in result.quality.gaps:
        print(f"GAP {gap.symbol} {gap.previous.isoformat()} -> {gap.current.isoformat()} ({gap.minutes:g} minutes)")
    if loaded.errors or result.rejected:
        sys.exit(1)


if __name__ == "__main__":
    main()
