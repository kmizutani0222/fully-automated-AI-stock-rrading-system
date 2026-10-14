from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.runtime import AccountModeChange


def record_mode_change(
    session: Session,
    *,
    account_id: int,
    previous_mode: str | None,
    new_mode: str,
    actor: str,
    action: str,
    request_id: str | None = None,
    reason: str | None = None,
    changed_at: datetime | None = None,
) -> None:
    """Insert one mode change. Existing rows are left as they were."""
    if previous_mode == new_mode:
        return
    session.add(
        AccountModeChange(
            account_id=account_id,
            previous_mode=previous_mode,
            new_mode=new_mode,
            changed_at=changed_at or datetime.now(timezone.utc),
            actor=actor,
            action=action,
            request_id=request_id,
            reason=reason,
        )
    )
