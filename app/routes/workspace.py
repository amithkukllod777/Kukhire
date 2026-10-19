"""Identity echo, workspaces, members, the HRMS link and the audit trail."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit
from app.auth import Principal, current_principal
from app.database import get_db
from app.db_models import AuditLog, Organization, OrganizationIntegration, OrganizationMember
from app.hrms_client import normalize_hrms_base_url
from app.schemas import IntegrationIn, MemberInvite, MemberUpdate, OrganizationCreate
from app.secrets_box import SecretBox, SecretBoxUnavailable
from app.tenancy import CAPABILITIES, MAX_ORGS_PER_USER, OrgContext, org_context, require

router = APIRouter(prefix="/v1", tags=["workspace"])


def _err(http_status: int, code: str, message: str) -> HTTPException:
    return HTTPException(http_status, detail={"error": code, "message": message})


def _claim_invitations(db: Session, principal: Principal) -> None:
    """A verified email picks up any workspace invitation addressed to it."""
    if not (principal.email and principal.email_verified):
        return
    pending = db.scalars(
        select(OrganizationMember).where(
            OrganizationMember.email == principal.email.strip().lower(),
            OrganizationMember.status == "invited",
            OrganizationMember.user_id.is_(None),
        )
    ).all()
    for row in pending:
        row.user_id = principal.user_id
        row.status = "active"
        try:
            db.commit()
        except IntegrityError:
            db.rollback()


def _capabilities_for(role: str) -> list[str]:
    return sorted(cap for cap, roles in CAPABILITIES.items() if role in roles)


@router.get("/me")
def me(principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    _claim_invitations(db, principal)
    rows = db.execute(
        select(Organization.id, Organization.name, OrganizationMember.role)
        .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .where(OrganizationMember.user_id == principal.user_id, OrganizationMember.status == "active")
        .order_by(Organization.name)
    ).all()
    return {
        "user": {"id": principal.user_id, "email": principal.email, "name": principal.name},
        "organizations": [{"id": r[0], "name": r[1], "role": r[2]} for r in rows],
    }


@router.post("/organizations", status_code=201)
def create_organization(
    body: OrganizationCreate,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    if not principal.email:
        raise _err(400, "email_required", "Add an email to your Kuklabs Account to create a workspace.")
    owned = db.scalar(
        select(func.count()).select_from(OrganizationMember).where(
            OrganizationMember.user_id == principal.user_id, OrganizationMember.role == "admin"
        )
    )
    if (owned or 0) >= MAX_ORGS_PER_USER:
        raise _err(409, "workspace_limit", f"An account can administer at most {MAX_ORGS_PER_USER} workspaces.")
    org = Organization(name=body.name, created_by_user_id=principal.user_id)
    db.add(org)
    db.flush()
    db.add(
        OrganizationMember(
            organization_id=org.id, user_id=principal.user_id, email=principal.email.strip().lower(),
            role="admin", status="active",
        )
    )
    audit.record(db, organization_id=org.id, actor_user_id=principal.user_id, action="organization.created",
                 entity_type="organization", entity_id=org.id)
    db.commit()
    return {"id": org.id, "name": org.name, "role": "admin"}


@router.get("/organizations/{org_id}")
def get_organization(ctx: OrgContext = Depends(org_context)):
    return {
        "id": ctx.org.id,
        "name": ctx.org.name,
        "role": ctx.role,
        "capabilities": _capabilities_for(ctx.role),
        "external_company_ref": ctx.org.external_company_ref if ctx.role == "admin" else None,
    }


# --- members ---------------------------------------------------------------------------------
def _member_out(m: OrganizationMember) -> dict:
    return {"id": m.id, "email": m.email, "role": m.role, "status": m.status, "user_id": m.user_id}


def _active_admins(db: Session, org_id: str) -> int:
    return int(
        db.scalar(
            select(func.count()).select_from(OrganizationMember).where(
                OrganizationMember.organization_id == org_id,
                OrganizationMember.role == "admin",
                OrganizationMember.status == "active",
            )
        )
        or 0
    )


@router.get("/organizations/{org_id}/members")
def list_members(ctx: OrgContext = Depends(require("org.manage")), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(OrganizationMember).where(OrganizationMember.organization_id == ctx.org.id).order_by(OrganizationMember.email)
    ).all()
    return {"items": [_member_out(m) for m in rows]}


@router.post("/organizations/{org_id}/members", status_code=201)
def invite_member(body: MemberInvite, ctx: OrgContext = Depends(require("org.manage")), db: Session = Depends(get_db)):
    email = str(body.email).strip().lower()
    member = OrganizationMember(
        organization_id=ctx.org.id, email=email, role=body.role, status="invited", invited_by_user_id=ctx.user_id
    )
    db.add(member)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise _err(409, "already_member", "That email is already a member or has a pending invitation.")
    audit.record(db, organization_id=ctx.org.id, actor_user_id=ctx.user_id, action="member.invited",
                 entity_type="member", entity_id=member.id, role=body.role)
    db.commit()
    return _member_out(member)


def _member_or_404(db: Session, ctx: OrgContext, member_id: str) -> OrganizationMember:
    m = db.scalar(
        select(OrganizationMember).where(
            OrganizationMember.id == member_id, OrganizationMember.organization_id == ctx.org.id
        )
    )
    if m is None:
        raise _err(404, "not_found", "Member not found.")
    return m


@router.patch("/organizations/{org_id}/members/{member_id}")
def update_member(
    member_id: str, body: MemberUpdate, ctx: OrgContext = Depends(require("org.manage")), db: Session = Depends(get_db)
):
    m = _member_or_404(db, ctx, member_id)
    if m.role == "admin" and body.role != "admin" and m.status == "active" and _active_admins(db, ctx.org.id) <= 1:
        raise _err(409, "last_admin", "A workspace needs at least one admin.")
    m.role = body.role
    audit.record(db, organization_id=ctx.org.id, actor_user_id=ctx.user_id, action="member.role_changed",
                 entity_type="member", entity_id=m.id, role=body.role)
    db.commit()
    return _member_out(m)


@router.delete("/organizations/{org_id}/members/{member_id}", status_code=204)
def remove_member(member_id: str, ctx: OrgContext = Depends(require("org.manage")), db: Session = Depends(get_db)):
    m = _member_or_404(db, ctx, member_id)
    if m.role == "admin" and m.status == "active" and _active_admins(db, ctx.org.id) <= 1:
        raise _err(409, "last_admin", "A workspace needs at least one admin.")
    audit.record(db, organization_id=ctx.org.id, actor_user_id=ctx.user_id, action="member.removed",
                 entity_type="member", entity_id=m.id)
    db.delete(m)
    db.commit()


# --- HRMS link -------------------------------------------------------------------------------
def _integration(db: Session, org_id: str) -> OrganizationIntegration | None:
    return db.scalar(select(OrganizationIntegration).where(OrganizationIntegration.organization_id == org_id))


@router.get("/organizations/{org_id}/integrations/hrms")
def get_hrms_link(ctx: OrgContext = Depends(require("org.manage")), db: Session = Depends(get_db)):
    link = _integration(db, ctx.org.id)
    if link is None:
        return {"configured": False}
    # The key is never returned — only whether one is stored and its last characters.
    return {"configured": True, "hrms_base_url": link.hrms_base_url, "api_key_hint": link.api_key_hint}


@router.put("/organizations/{org_id}/integrations/hrms")
def put_hrms_link(
    body: IntegrationIn, request: Request, ctx: OrgContext = Depends(require("org.manage")), db: Session = Depends(get_db)
):
    settings = request.app.state.settings
    try:
        base = normalize_hrms_base_url(
            body.hrms_base_url,
            allowed_hosts=settings.hrms_allowed_hosts,
            allow_insecure_http=settings.hrms_allow_insecure_http,
        )
    except ValueError as exc:
        raise _err(422, "invalid_hrms_url", str(exc))
    try:
        encrypted = SecretBox(settings.secret_encryption_key).encrypt(body.api_key)
    except SecretBoxUnavailable:
        raise _err(503, "secret_storage_unavailable", "This deployment cannot store secrets yet (encryption key not set).")
    link = _integration(db, ctx.org.id)
    if link is None:
        link = OrganizationIntegration(
            organization_id=ctx.org.id, hrms_base_url=base, encrypted_api_key=encrypted,
            api_key_hint=body.api_key[-4:], created_by_user_id=ctx.user_id,
        )
        db.add(link)
    else:
        link.hrms_base_url, link.encrypted_api_key, link.api_key_hint = base, encrypted, body.api_key[-4:]
    audit.record(db, organization_id=ctx.org.id, actor_user_id=ctx.user_id, action="integration.hrms_saved",
                 entity_type="organization", entity_id=ctx.org.id)
    db.commit()
    return {"configured": True, "hrms_base_url": link.hrms_base_url, "api_key_hint": link.api_key_hint}


@router.delete("/organizations/{org_id}/integrations/hrms", status_code=204)
def delete_hrms_link(ctx: OrgContext = Depends(require("org.manage")), db: Session = Depends(get_db)):
    link = _integration(db, ctx.org.id)
    if link is not None:
        db.delete(link)
        audit.record(db, organization_id=ctx.org.id, actor_user_id=ctx.user_id, action="integration.hrms_removed",
                     entity_type="organization", entity_id=ctx.org.id)
        db.commit()


# --- audit -----------------------------------------------------------------------------------
@router.get("/organizations/{org_id}/audit")
def read_audit(ctx: OrgContext = Depends(require("audit.read")), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(AuditLog).where(AuditLog.organization_id == ctx.org.id).order_by(AuditLog.created_at.desc()).limit(200)
    ).all()
    return {
        "items": [
            {
                "id": r.id, "action": r.action, "entity_type": r.entity_type, "entity_id": r.entity_id,
                "actor_user_id": r.actor_user_id, "detail": r.detail, "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]
    }
