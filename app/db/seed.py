from __future__ import annotations

from pathlib import Path

import yaml
from sqlalchemy.orm import Session

from sqlalchemy import select

from app.data.repository import ensure_instrument
from app.models.trading import Account, CashLedgerEntry, StrategyRecord
from app.trading.config import load_trading_config
from app.trading.strategies import strategy_names

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CATALOG = PROJECT_ROOT / "config" / "instruments.yaml"


def seed_instruments(session: Session, catalog: Path = DEFAULT_CATALOG) -> int:
    rows = yaml.safe_load(catalog.read_text(encoding="utf-8")) or []
    for row in rows:
        ensure_instrument(
            session,
            market=str(row["market"]),
            symbol=str(row["symbol"]),
            name=row.get("name"),
            exchange=row.get("exchange"),
            currency=row.get("currency"),
        )
    seed_paper_account(session)
    session.commit()
    return len(rows)


def seed_paper_account(session: Session) -> None:
    """Create the paper account once. Later restarts do not reset cash or positions."""
    config = load_trading_config()
    account = session.scalar(select(Account).where(Account.name == "paper"))
    if account is None:
        account = Account(name="paper")
        session.add(account)
        session.flush()
    existing = session.scalar(select(CashLedgerEntry.id).where(CashLedgerEntry.account_id == account.id).limit(1))
    if existing is None:
        for currency, amount in config.cash().items():
            session.add(
                CashLedgerEntry(
                    account_id=account.id,
                    currency=currency,
                    amount=amount,
                    balance_after=amount,
                    reason="INITIAL",
                )
            )
    for name in strategy_names():
        found = session.scalar(
            select(StrategyRecord).where(StrategyRecord.name == name, StrategyRecord.version == config.strategy_version)
        )
        if found is None:
            session.add(StrategyRecord(name=name, version=config.strategy_version, parameters={}))
    session.flush()
