"""Audit trail. Records WHO did WHAT to WHICH record — identifiers and field names only.

Never candidate content: no names, emails, resume text, notes or scores' reasoning. An audit row
must be safe to keep after the candidate it mentions has been erased.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.db_models import AuditLog


def record(
    db: Session,
    *,
    organization_id: str | None,
    actor_user_id: str | None,
    action: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    **detail,
) -> None:
    """Add an audit row to the caller's session; it commits with the caller's transaction."""
    db.add(
        AuditLog(
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            detail=detail,
        )
    )
