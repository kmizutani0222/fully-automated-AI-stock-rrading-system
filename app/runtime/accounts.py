from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.trading import Account, CashLedgerEntry
from app.runtime.schedule import FILL_MODEL
from app.models.runtime import TradingSession

MODES = {"RULE_BASED", "AI", "HYBRID", "PAUSED"}


def cash_balances(session: Session, account_id: int) -> dict[str, Decimal]:
    rows = session.scalars(
        select(CashLedgerEntry).where(CashLedgerEntry.account_id == account_id).order_by(CashLedgerEntry.id)
    ).all()
    balances: dict[str, Decimal] = {}
    for row in rows:
        balances[row.currency] = row.balance_after
    return balances


def create_account(
    session: Session,
    *,
    name: str,
    base_currency: str,
    jpy_cash: Decimal,
    usd_cash: Decimal,
    markets: list[str],
    strategies: list[str],
    symbols: list[str],
    mode: str,
    fallback_to_rules: bool,
    risk_settings: dict,
) -> Account:
    if mode not in MODES:
        raise ValueError("mode must be RULE_BASED, AI, HYBRID, or PAUSED")
    if session.scalar(select(Account).where(Account.name == name)) is not None:
        raise ValueError("account name already exists")
    account = Account(
        name=name,
        base_currency=base_currency,
        markets=markets,
        mode=mode,
        strategies=strategies,
        symbols=symbols,
        risk_settings=risk_settings,
        fallback_to_rules=fallback_to_rules,
    )
    session.add(account)
    session.flush()
    _deposit_initial(session, account.id, "JPY", jpy_cash)
    _deposit_initial(session, account.id, "USD", usd_cash)
    now = datetime.now(timezone.utc)
    session.add(
        TradingSession(
            account_id=account.id,
            mode=mode,
            status="RUNNING" if mode != "PAUSED" else "PAUSED",
            resume_mode="RULE_BASED" if mode == "PAUSED" else mode,
            fill_model=FILL_MODEL,
            started_at=now,
            updated_at=now,
        )
    )
    session.flush()
    return account


def post_cash(session: Session, account: Account, currency: str, amount: Decimal, reason: str) -> CashLedgerEntry:
    if currency not in {"JPY", "USD"}:
        raise ValueError("currency must be JPY or USD")
    if reason not in {"DEPOSIT", "WITHDRAWAL"}:
        raise ValueError("cash changes are deposit or withdrawal events")
    if amount <= 0:
        raise ValueError("amount must be positive")
    balances = cash_balances(session, account.id)
    current = balances.get(currency, Decimal(0))
    if reason == "WITHDRAWAL" and current < amount:
        raise ValueError("withdrawal exceeds the currency balance")
    signed = amount if reason == "DEPOSIT" else -amount
    entry = CashLedgerEntry(
        account_id=account.id,
        currency=currency,
        amount=signed,
        balance_after=current + signed,
        reason=reason,
    )
    session.add(entry)
    session.flush()
    return entry


def _deposit_initial(session: Session, account_id: int, currency: str, amount: Decimal) -> None:
    if amount < 0:
        raise ValueError("initial cash cannot be negative")
    if amount == 0:
        return
    session.add(
        CashLedgerEntry(
            account_id=account_id,
            currency=currency,
            amount=amount,
            balance_after=amount,
            reason="DEPOSIT",
        )
    )


def latest_session(session: Session, account_id: int) -> TradingSession | None:
    return session.scalar(
        select(TradingSession).where(TradingSession.account_id == account_id).order_by(TradingSession.id.desc())
    )
