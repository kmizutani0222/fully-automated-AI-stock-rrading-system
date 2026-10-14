"""Revision ID: 005_paper_validation
Revises: 004_runtime
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "005_paper_validation"
down_revision: str | None = "004_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("symbol_strategies", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")))
    op.add_column("orders", sa.Column("submitted_at", sa.DateTime(timezone=True)))
    op.add_column("orders", sa.Column("signal_generated_at", sa.DateTime(timezone=True)))
    op.add_column("orders", sa.Column("bar_close_time", sa.DateTime(timezone=True)))
    op.add_column("orders", sa.Column("data_received_at", sa.DateTime(timezone=True)))
    op.add_column("orders", sa.Column("strategy_name", sa.String(50)))
    op.add_column("orders", sa.Column("decision_reason", sa.Text()))
    op.add_column("executions", sa.Column("fill_observed_at", sa.DateTime(timezone=True)))
    op.add_column("executions", sa.Column("filled_at", sa.DateTime(timezone=True)))
    op.add_column("cash_ledger", sa.Column("idempotency_key", sa.String(200)))
    op.create_index("uq_cash_ledger_idempotency_key", "cash_ledger", ["idempotency_key"], unique=True)
    op.create_table(
        "market_data_health",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("market", sa.String(20), nullable=False),
        sa.Column("symbol", sa.String(30), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("latest_bar_at", sa.DateTime(timezone=True)),
        sa.Column("lag_seconds", sa.Integer()),
        sa.Column("gap_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("message", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_market_data_health_symbol", "market_data_health", ["market", "symbol"])
    op.create_table(
        "paper_execution_observations",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("order_id", sa.BigInteger, sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("bar_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True)),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("reference_price", sa.Numeric(20, 8)),
        sa.Column("fill_price", sa.Numeric(20, 8)),
        sa.Column("accepted", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_paper_execution_observations_order", "paper_execution_observations", ["order_id"])
    op.create_table(
        "daily_performance",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("account_id", sa.BigInteger, sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("trading_day", sa.DateTime(timezone=True), nullable=False),
        sa.Column("currency", sa.String(10), nullable=False),
        sa.Column("equity", sa.Numeric(20, 8)),
        sa.Column("realized_pnl", sa.Numeric(20, 8)),
        sa.Column("unrealized_pnl", sa.Numeric(20, 8)),
        sa.Column("day_pnl", sa.Numeric(20, 8)),
        sa.Column("max_drawdown", sa.Numeric(20, 8)),
        sa.Column("trade_count", sa.Integer()),
        sa.Column("win_rate", sa.Numeric(20, 8)),
        sa.Column("cost_total", sa.Numeric(20, 8)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("account_id", "trading_day", "currency", name="uq_daily_performance_account_day"),
    )


def downgrade() -> None:
    op.drop_table("daily_performance")
    op.drop_table("paper_execution_observations")
    op.drop_table("market_data_health")
    op.drop_index("uq_cash_ledger_idempotency_key", table_name="cash_ledger")
    op.drop_column("cash_ledger", "idempotency_key")
    op.drop_column("executions", "filled_at")
    op.drop_column("executions", "fill_observed_at")
    op.drop_column("orders", "decision_reason")
    op.drop_column("orders", "strategy_name")
    op.drop_column("orders", "data_received_at")
    op.drop_column("orders", "bar_close_time")
    op.drop_column("orders", "signal_generated_at")
    op.drop_column("orders", "submitted_at")
    op.drop_column("accounts", "symbol_strategies")
