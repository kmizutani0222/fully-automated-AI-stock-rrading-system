"""Revision ID: 004_runtime
Revises: 003_ai
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "004_runtime"
down_revision: str | None = "003_ai"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("base_currency", sa.String(10), nullable=False, server_default="JPY"))
    op.add_column("accounts", sa.Column("markets", sa.JSON(), nullable=False, server_default=sa.text("""'["US"]'::json""")))
    op.add_column("accounts", sa.Column("mode", sa.String(20), nullable=False, server_default="PAUSED"))
    op.add_column("accounts", sa.Column("strategies", sa.JSON(), nullable=False, server_default=sa.text("""'["ema_cross"]'::json""")))
    op.add_column("accounts", sa.Column("symbols", sa.JSON(), nullable=False, server_default=sa.text("""'["AAPL"]'::json""")))
    op.add_column("accounts", sa.Column("risk_settings", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")))
    op.add_column("accounts", sa.Column("fallback_to_rules", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("orders", sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("accounts.id", ondelete="CASCADE")))
    op.add_column("orders", sa.Column("execute_at", sa.DateTime(timezone=True)))
    op.add_column("orders", sa.Column("reserved_cash", sa.Numeric(20, 8)))
    op.create_index("ix_orders_account", "orders", ["account_id"])
    op.create_table(
        "scheduler_jobs",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("name", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("locked_by", sa.String(80)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("name", name="uq_scheduler_jobs_name"),
    )
    op.create_table(
        "job_executions",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("job_id", sa.BigInteger, sa.ForeignKey("scheduler_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("bar_timestamp", sa.DateTime(timezone=True)),
        sa.Column("reason", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_job_executions_job", "job_executions", ["job_id"])
    op.create_table(
        "processing_checkpoints",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("account_id", sa.BigInteger, sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("market", sa.String(20), nullable=False),
        sa.Column("symbol", sa.String(30), nullable=False),
        sa.Column("bar_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("account_id", "market", "symbol", "bar_timestamp", name="uq_processing_checkpoints_bar"),
    )
    op.create_index("ix_processing_checkpoints_account", "processing_checkpoints", ["account_id", "symbol"])
    op.create_table(
        "trading_sessions",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("account_id", sa.BigInteger, sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("resume_mode", sa.String(20)),
        sa.Column("fill_model", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_trading_sessions_account", "trading_sessions", ["account_id"])
    op.create_table(
        "notification_events",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("account_id", sa.BigInteger, sa.ForeignKey("accounts.id", ondelete="CASCADE")),
        sa.Column("level", sa.String(20), nullable=False),
        sa.Column("code", sa.String(60), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_notification_events_created", "notification_events", ["created_at"])
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("actor", sa.String(80), nullable=False),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("path", sa.String(200), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_audit_logs_created", "audit_logs", ["created_at"])


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("notification_events")
    op.drop_table("trading_sessions")
    op.drop_table("processing_checkpoints")
    op.drop_table("job_executions")
    op.drop_table("scheduler_jobs")
    op.drop_index("ix_orders_account", table_name="orders")
    op.drop_column("orders", "reserved_cash")
    op.drop_column("orders", "execute_at")
    op.drop_column("orders", "account_id")
    op.drop_column("accounts", "fallback_to_rules")
    op.drop_column("accounts", "risk_settings")
    op.drop_column("accounts", "symbols")
    op.drop_column("accounts", "strategies")
    op.drop_column("accounts", "mode")
    op.drop_column("accounts", "markets")
    op.drop_column("accounts", "base_currency")
