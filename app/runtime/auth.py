from __future__ import annotations

import secrets

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_session
from app.models.runtime import AuditLog


def require_admin(authorization: str | None = Header(default=None)) -> str:
    expected = get_settings().admin_token
    supplied = ""
    if authorization and authorization.startswith("Bearer "):
        supplied = authorization.removeprefix("Bearer ").strip()
    if not expected or not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="authentication required")
    return "admin"


def write_audit(session: Session, actor: str, action: str, path: str, details: dict) -> None:
    session.add(AuditLog(actor=actor, action=action, path=path, details=details))


def admin_session(actor: str = Depends(require_admin), session: Session = Depends(get_session)) -> tuple[str, Session]:
    return actor, session
