"""Revision ID: 006_mode_history
Revises: 005_paper_validation
Create Date: 2026-10-14
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "006_mode_history"
down_revision: str | None = "005_paper_validation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "account_mode_changes",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("account_id", sa.BigInteger, sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("previous_mode", sa.String(20)),
        sa.Column("new_mode", sa.String(20), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(80), nullable=False),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("request_id", sa.String(120)),
        sa.Column("reason", sa.Text()),
    )
    op.create_index("ix_account_mode_changes_account", "account_mode_changes", ["account_id", "changed_at"])
    op.add_column("positions", sa.Column("stop_price", sa.Numeric(20, 8)))
    op.add_column("positions", sa.Column("take_profit", sa.Numeric(20, 8)))
    _backfill_from_audit(op.get_bind())


def downgrade() -> None:
    op.drop_column("positions", "take_profit")
    op.drop_column("positions", "stop_price")
    op.drop_index("ix_account_mode_changes_account", table_name="account_mode_changes")
    op.drop_table("account_mode_changes")


def _backfill_from_audit(bind) -> None:
    rows = bind.execute(
        sa.text(
            """
            SELECT actor, action, details, created_at
            FROM audit_logs
            WHERE action IN ('runtime_pause', 'runtime_resume', 'runtime_start')
            ORDER BY id
            """
        )
    ).mappings()
    last_mode: dict[int, str] = {}
    for row in rows:
        details = row["details"] or {}
        if isinstance(details, str):
            details = json.loads(details)
        account_raw = details.get("account_id")
        if account_raw is None:
            continue
        account_id = int(account_raw)
        if row["action"] == "runtime_pause":
            new_mode = "PAUSED"
        else:
            new_mode = str(details.get("mode") or "RULE_BASED")
        previous = last_mode.get(account_id)
        last_mode[account_id] = new_mode
        if previous == new_mode:
            continue
        bind.execute(
            sa.text(
                """
                INSERT INTO account_mode_changes
                    (account_id, previous_mode, new_mode, changed_at, actor, action, request_id, reason)
                VALUES
                    (:account_id, :previous_mode, :new_mode, :changed_at, :actor, :action, NULL, :reason)
                """
            ),
            {
                "account_id": account_id,
                "previous_mode": previous,
                "new_mode": new_mode,
                "changed_at": row["created_at"],
                "actor": row["actor"],
                "action": row["action"],
                "reason": "backfill_from_audit",
            },
        )
