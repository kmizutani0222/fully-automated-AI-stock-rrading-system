"""Revision ID: 002_trading
Revises: 001_initial
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "002_trading"
down_revision: str | None = "001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("name", sa.String(50), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "fx_rates",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("base_currency", sa.String(10), nullable=False),
        sa.Column("quote_currency", sa.String(10), nullable=False),
        sa.Column("rate", sa.Numeric(20, 8), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("base_currency", "quote_currency", "as_of", name="uq_fx_rates_pair_time"),
    )
    op.create_table(
        "strategies",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("name", sa.String(50), nullable=False),
        sa.Column("version", sa.String(30), nullable=False),
        sa.Column("parameters", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("name", "version", name="uq_strategies_name_version"),
    )
    op.create_table(
        "backtest_runs",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("strategy_name", sa.String(50), nullable=False),
        sa.Column("strategy_version", sa.String(30), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("market", sa.String(20), nullable=False),
        sa.Column("timeframe", sa.String(10), nullable=False),
        sa.Column("symbols", sa.JSON, nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("parameters", sa.JSON, nullable=False),
        sa.Column("initial_cash", sa.JSON, nullable=False),
        sa.Column("metrics", sa.JSON),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "strategy_signals",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("strategy_name", sa.String(50), nullable=False),
        sa.Column("instrument_id", sa.BigInteger, sa.ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("backtest_run_id", sa.BigInteger, sa.ForeignKey("backtest_runs.id", ondelete="CASCADE")),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("signal", sa.String(10), nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_strategy_signals_run", "strategy_signals", ["backtest_run_id"])
    op.create_table(
        "orders",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("backtest_run_id", sa.BigInteger, sa.ForeignKey("backtest_runs.id", ondelete="CASCADE")),
        sa.Column("instrument_id", sa.BigInteger, sa.ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("order_type", sa.String(20), nullable=False),
        sa.Column("quantity", sa.Numeric(30, 8), nullable=False),
        sa.Column("filled_quantity", sa.Numeric(30, 8), nullable=False),
        sa.Column("limit_price", sa.Numeric(20, 8)),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_orders_idempotency_key"),
    )
    op.create_table(
        "order_events",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("order_id", sa.BigInteger, sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "executions",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("order_id", sa.BigInteger, sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.Integer, nullable=False),
        sa.Column("quantity", sa.Numeric(30, 8), nullable=False),
        sa.Column("price", sa.Numeric(20, 8), nullable=False),
        sa.Column("commission", sa.Numeric(20, 8), nullable=False),
        sa.Column("slippage_cost", sa.Numeric(20, 8), nullable=False),
        sa.Column("spread_cost", sa.Numeric(20, 8), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("order_id", "sequence", name="uq_executions_order_sequence"),
    )
    op.create_table(
        "backtest_trades",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("backtest_run_id", sa.BigInteger, sa.ForeignKey("backtest_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("instrument_id", sa.BigInteger, sa.ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("quantity", sa.Numeric(30, 8), nullable=False),
        sa.Column("signal_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fill_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reference_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("fill_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("commission", sa.Numeric(20, 8), nullable=False),
        sa.Column("slippage_cost", sa.Numeric(20, 8), nullable=False),
        sa.Column("spread_cost", sa.Numeric(20, 8), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(20, 8)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "positions",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("backtest_run_id", sa.BigInteger, sa.ForeignKey("backtest_runs.id", ondelete="CASCADE")),
        sa.Column("account_id", sa.BigInteger, sa.ForeignKey("accounts.id", ondelete="CASCADE")),
        sa.Column("instrument_id", sa.BigInteger, sa.ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("quantity", sa.Numeric(30, 8), nullable=False),
        sa.Column("average_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(20, 8), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "cash_ledger",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("account_id", sa.BigInteger, sa.ForeignKey("accounts.id", ondelete="CASCADE")),
        sa.Column("backtest_run_id", sa.BigInteger, sa.ForeignKey("backtest_runs.id", ondelete="CASCADE")),
        sa.Column("currency", sa.String(10), nullable=False),
        sa.Column("amount", sa.Numeric(20, 8), nullable=False),
        sa.Column("balance_after", sa.Numeric(20, 8), nullable=False),
        sa.Column("reason", sa.String(50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "portfolio_snapshots",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("account_id", sa.BigInteger, sa.ForeignKey("accounts.id", ondelete="CASCADE")),
        sa.Column("backtest_run_id", sa.BigInteger, sa.ForeignKey("backtest_runs.id", ondelete="CASCADE")),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("currency", sa.String(10), nullable=False),
        sa.Column("cash", sa.Numeric(20, 8), nullable=False),
        sa.Column("market_value", sa.Numeric(20, 8), nullable=False),
        sa.Column("equity", sa.Numeric(20, 8)),
        sa.Column("drawdown", sa.Numeric(20, 8)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_portfolio_snapshots_run", "portfolio_snapshots", ["backtest_run_id"])
    op.create_table(
        "risk_events",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("backtest_run_id", sa.BigInteger, sa.ForeignKey("backtest_runs.id", ondelete="CASCADE")),
        sa.Column("code", sa.String(40), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("details", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    for name in (
        "risk_events",
        "portfolio_snapshots",
        "cash_ledger",
        "positions",
        "backtest_trades",
        "executions",
        "order_events",
        "orders",
        "strategy_signals",
        "backtest_runs",
        "strategies",
        "fx_rates",
        "accounts",
    ):
        op.drop_table(name)
