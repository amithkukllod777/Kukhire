"""Workspace (organization) access control.

The shared Kuklabs Account tells us WHO is calling. Which workspaces that person may open, and as
what, is KukHire's own data (``organization_members``). Every route that touches workspace data goes
through :func:`org_context`, so:

* the organization id in the URL is never trusted on its own — it is only honoured if the caller
  has an active membership in it;
* a caller with no membership gets **404**, not 403, so the existence of other workspaces cannot be
  probed;
* what the caller may do is a capability check on their role, enforced per route.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import Principal, current_principal
from app.database import get_db
from app.db_models import Organization, OrganizationMember

ROLES = ("admin", "recruiter", "interviewer", "viewer")

_ALL = frozenset(ROLES)
_RECRUITING = frozenset({"admin", "recruiter"})

CAPABILITIES: dict[str, frozenset[str]] = {
    "org.manage": frozenset({"admin"}),
    "audit.read": frozenset({"admin"}),
    "candidates.erase": frozenset({"admin"}),
    "jobs.read": _ALL,
    "jobs.write": _RECRUITING,
    "candidates.read": _ALL,
    "candidates.write": _RECRUITING,
    "applications.read": _ALL,
    "applications.write": _RECRUITING,
    "applications.transition": _RECRUITING,
    # Interviewers write feedback but only read their own, so one interviewer's view does not
    # anchor another's; recruiters and admins see every scorecard.
    "scorecards.write": frozenset({"admin", "recruiter", "interviewer"}),
    "scorecards.read_all": _RECRUITING,
    "scorecards.read_own": frozenset({"interviewer"}),
    "handoff.request": _RECRUITING,
    "evaluation.run": _RECRUITING,
    "evaluation.read": _RECRUITING,
}

MAX_ORGS_PER_USER = 20


def can(role: str, capability: str) -> bool:
    return role in CAPABILITIES.get(capability, frozenset())


@dataclass(frozen=True)
class OrgContext:
    org: Organization
    member: OrganizationMember
    principal: Principal

    @property
    def role(self) -> str:
        return self.member.role

    @property
    def user_id(self) -> str:
        return self.principal.user_id


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, detail={"error": "not_found", "message": "Workspace not found."})


def resolve_member(db: Session, org_id: str, principal: Principal) -> OrganizationMember | None:
    """The caller's active membership in `org_id`, claiming a pending invitation if one matches a
    VERIFIED email. Returns None when the caller has no business in this workspace."""
    member = db.scalar(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == principal.user_id,
            OrganizationMember.status == "active",
        )
    )
    if member is not None:
        return member
    if not (principal.email and principal.email_verified):
        return None
    invited = db.scalar(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.email == principal.email.strip().lower(),
            OrganizationMember.status == "invited",
            OrganizationMember.user_id.is_(None),
        )
    )
    if invited is None:
        return None
    invited.user_id = principal.user_id
    invited.status = "active"
    try:
        db.commit()
    except IntegrityError:
        # The same account claimed two invitations in one workspace concurrently; the unique
        # index on (organization_id, user_id) kept one. Re-read what won.
        db.rollback()
        return db.scalar(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == org_id,
                OrganizationMember.user_id == principal.user_id,
                OrganizationMember.status == "active",
            )
        )
    return invited


def org_context(
    org_id: str,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
) -> OrgContext:
    org = db.get(Organization, org_id)
    if org is None:
        raise _not_found()
    member = resolve_member(db, org_id, principal)
    if member is None:
        raise _not_found()
    return OrgContext(org=org, member=member, principal=principal)


def require(capability: str):
    """Route dependency: the caller is a member AND their role grants `capability`."""
    if capability not in CAPABILITIES:  # a typo here must fail at import, not grant nothing silently
        raise KeyError(f"unknown capability {capability!r}")

    def dependency(ctx: OrgContext = Depends(org_context)) -> OrgContext:
        if not can(ctx.role, capability):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail={"error": "forbidden", "message": "Your role does not allow this action."},
            )
        return ctx

    return dependency
