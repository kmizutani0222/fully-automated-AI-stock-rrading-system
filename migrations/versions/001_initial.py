"""Revision ID: 001_initial
Revises:
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "instruments",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("market", sa.String(20), nullable=False),
        sa.Column("symbol", sa.String(30), nullable=False),
        sa.Column("name", sa.String(255)),
        sa.Column("exchange", sa.String(50)),
        sa.Column("currency", sa.String(10), nullable=False),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("market", "symbol", name="uq_instruments_market_symbol"),
    )
    op.create_table(
        "market_bars",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("instrument_id", sa.BigInteger, sa.ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("timeframe", sa.String(10), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(20, 8), nullable=False),
        sa.Column("high", sa.Numeric(20, 8), nullable=False),
        sa.Column("low", sa.Numeric(20, 8), nullable=False),
        sa.Column("close", sa.Numeric(20, 8), nullable=False),
        sa.Column("volume", sa.Numeric(30, 8)),
        sa.Column("vwap", sa.Numeric(20, 8)),
        sa.Column("trade_count", sa.BigInteger),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("is_adjusted", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "instrument_id",
            "timeframe",
            "timestamp",
            "provider",
            name="uq_market_bars_instrument_time_provider",
        ),
    )
    op.create_index("ix_market_bars_instrument_id", "market_bars", ["instrument_id"])
    op.create_index("ix_market_bars_timestamp", "market_bars", ["timestamp"])
    op.create_index("ix_market_bars_instrument_timestamp", "market_bars", ["instrument_id", "timestamp"])


def downgrade() -> None:
    op.drop_index("ix_market_bars_instrument_timestamp", table_name="market_bars")
    op.drop_index("ix_market_bars_timestamp", table_name="market_bars")
    op.drop_index("ix_market_bars_instrument_id", table_name="market_bars")
    op.drop_table("market_bars")
    op.drop_table("instruments")
