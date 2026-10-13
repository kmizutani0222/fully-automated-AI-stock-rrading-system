"""Revision ID: 003_ai
Revises: 002_trading
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "003_ai"
down_revision: str | None = "002_trading"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "feature_snapshots",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("instrument_id", sa.BigInteger, sa.ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("feature_version", sa.String(30), nullable=False),
        sa.Column("values", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("instrument_id", "timestamp", "feature_version", name="uq_feature_snapshots_bar"),
    )
    op.create_index("ix_feature_snapshots_instrument_time", "feature_snapshots", ["instrument_id", "timestamp"])
    op.create_table(
        "market_regimes",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("instrument_id", sa.BigInteger, sa.ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("regime", sa.String(20), nullable=False),
        sa.Column("method", sa.String(20), nullable=False),
        sa.Column("details", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("instrument_id", "timestamp", "method", name="uq_market_regimes_bar_method"),
    )
    op.create_index("ix_market_regimes_instrument_time", "market_regimes", ["instrument_id", "timestamp"])
    op.create_table(
        "model_versions",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("task", sa.String(20), nullable=False),
        sa.Column("version", sa.String(80), nullable=False),
        sa.Column("family", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("feature_version", sa.String(30), nullable=False),
        sa.Column("train_start", sa.DateTime(timezone=True)),
        sa.Column("train_end", sa.DateTime(timezone=True)),
        sa.Column("test_start", sa.DateTime(timezone=True)),
        sa.Column("test_end", sa.DateTime(timezone=True)),
        sa.Column("parameters", sa.JSON, nullable=False),
        sa.Column("artifact_path", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("task", "version", name="uq_model_versions_task_version"),
    )
    op.create_table(
        "training_runs",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("task", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("market", sa.String(20), nullable=False),
        sa.Column("symbols", sa.JSON, nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("parameters", sa.JSON, nullable=False),
        sa.Column("reason", sa.Text),
        sa.Column("model_version_id", sa.BigInteger, sa.ForeignKey("model_versions.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "model_metrics",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("model_version_id", sa.BigInteger, sa.ForeignKey("model_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("label", sa.String(40), nullable=False),
        sa.Column("metric_name", sa.String(40), nullable=False),
        sa.Column("metric_value", sa.Numeric(20, 8)),
        sa.Column("sample_count", sa.Integer),
    )
    op.create_table(
        "predictions",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("model_version_id", sa.BigInteger, sa.ForeignKey("model_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("instrument_id", sa.BigInteger, sa.ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("predicted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("horizon", sa.String(40), nullable=False),
        sa.Column("expected_return", sa.Numeric(20, 8)),
        sa.Column("up_probability", sa.Numeric(20, 8)),
        sa.Column("risk", sa.Numeric(20, 8)),
        sa.Column("gap_risk", sa.Numeric(20, 8)),
        sa.Column("is_out_of_sample", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_predictions_instrument_time", "predictions", ["instrument_id", "available_at"])
    op.create_table(
        "strategy_decisions",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("instrument_id", sa.BigInteger, sa.ForeignKey("instruments.id", ondelete="CASCADE")),
        sa.Column("backtest_run_id", sa.BigInteger, sa.ForeignKey("backtest_runs.id", ondelete="CASCADE")),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("choice", sa.String(10), nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("score", sa.Numeric(20, 8)),
        sa.Column("details", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_strategy_decisions_time", "strategy_decisions", ["timestamp"])
    op.create_table(
        "walk_forward_runs",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("training_run_id", sa.BigInteger, sa.ForeignKey("training_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("fold", sa.Integer, nullable=False),
        sa.Column("train_start", sa.DateTime(timezone=True)),
        sa.Column("train_end", sa.DateTime(timezone=True)),
        sa.Column("test_start", sa.DateTime(timezone=True)),
        sa.Column("test_end", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("metrics", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    for name in (
        "walk_forward_runs",
        "strategy_decisions",
        "predictions",
        "model_metrics",
        "training_runs",
        "model_versions",
        "market_regimes",
        "feature_snapshots",
    ):
        op.drop_table(name)
