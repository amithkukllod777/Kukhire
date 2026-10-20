"""Hiring handoff service: turn a HIRED application into an HRMS employee reference.

Ownership: HRMS creates the employee and runs onboarding. This service only (a) enforces that the
application is actually hired, (b) sends the minimum HRMS needs — identity, role, start date,
referral attribution — with a stable idempotency reference, and (c) records the reference HRMS
returns. It never sends salary, never writes employee tables, and never claims success it did not
receive.
"""
from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit
from app.config import Settings
from app.db_models import (
    Application,
    Candidate,
    HiringHandoff,
    Job,
    LegacyImportMap,
    OrganizationIntegration,
    utcnow,
)
from app.hrms_client import HandoffRejected, HandoffTransient, HrmsClient
from app.secrets_box import SecretBox, SecretBoxUnavailable
from app.tenancy import OrgContext

LEGACY_SYSTEM = "kukbook-erp"

ClientFactory = Callable[[str, str], HrmsClient]


class HandoffUnavailable(Exception):
    """A precondition failed. `code` is a stable machine-readable reason; `message` is user-safe."""

    def __init__(self, code: str, message: str, http_status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def external_ref_for(application_id: str) -> str:
    return f"kukhire:{application_id}"


def _int_or_none(value: str | None) -> int | None:
    return int(value) if value and value.isdigit() else None


def serialize(handoff: HiringHandoff | None) -> dict | None:
    if handoff is None:
        return None
    return {
        "id": handoff.id,
        "status": handoff.status,
        "employee_ref": handoff.employee_ref,
        "employee_code": handoff.employee_code,
        "attempts": handoff.attempts,
        "last_error": handoff.last_error,
        "external_ref": handoff.external_ref,
        "start_date": handoff.start_date.isoformat() if handoff.start_date else None,
        "completed_at": handoff.completed_at.isoformat() if handoff.completed_at else None,
    }


def request_handoff(
    db: Session,
    ctx: OrgContext,
    application: Application,
    *,
    start_date,
    settings: Settings,
    client_factory: ClientFactory | None = None,
) -> HiringHandoff:
    if application.organization_id != ctx.org.id:  # defence in depth; the route already scoped it
        raise HandoffUnavailable("not_found", "Application not found.", 404)
    if application.stage != "hired":
        raise HandoffUnavailable("not_hired", "Only a hired application can be handed to HRMS.")

    existing = db.scalar(select(HiringHandoff).where(HiringHandoff.application_id == application.id))
    if existing is not None and existing.status == "completed":
        return existing  # nothing to do — and nothing to re-send

    integration = db.scalar(
        select(OrganizationIntegration).where(OrganizationIntegration.organization_id == ctx.org.id)
    )
    if integration is None:
        raise HandoffUnavailable("hrms_not_configured", "This workspace is not linked to an HRMS yet.")
    try:
        api_key = SecretBox(settings.secret_encryption_key).decrypt(integration.encrypted_api_key)
    except SecretBoxUnavailable:
        raise HandoffUnavailable(
            "secret_unavailable", "The stored HRMS credential cannot be read; re-enter it.", 503
        )

    candidate = db.get(Candidate, application.candidate_id)
    job = db.get(Job, application.job_id)
    if candidate is None or job is None:
        raise HandoffUnavailable("not_found", "Application data is incomplete.", 409)

    if existing is None:
        existing = HiringHandoff(
            organization_id=ctx.org.id,
            application_id=application.id,
            external_ref=external_ref_for(application.id),
            status="pending",
            requested_by_user_id=ctx.user_id,
            start_date=start_date,
        )
        db.add(existing)
        try:
            db.flush()
        except IntegrityError:
            # A concurrent request created it first; use theirs (the unique key on application_id).
            db.rollback()
            existing = db.scalar(select(HiringHandoff).where(HiringHandoff.application_id == application.id))
            if existing is None:
                raise
            if existing.status == "completed":
                return existing
    if start_date is not None:
        existing.start_date = start_date
    existing.attempts += 1
    existing.status = "pending"
    existing.requested_by_user_id = ctx.user_id
    db.commit()  # the attempt is on record BEFORE we call out

    legacy = db.scalar(
        select(LegacyImportMap).where(
            LegacyImportMap.organization_id == ctx.org.id,
            LegacyImportMap.source_system == LEGACY_SYSTEM,
            LegacyImportMap.source_table == "candidates",
            LegacyImportMap.target_table == "applications",
            LegacyImportMap.target_id == application.id,
        )
    )
    payload: dict = {
        "externalApplicationRef": existing.external_ref,
        "name": candidate.full_name,
        "jobTitle": job.title[:100],
    }
    if candidate.email:
        payload["email"] = candidate.email
    if job.department:
        payload["department"] = job.department[:100]
    if existing.start_date:
        payload["startDate"] = existing.start_date.isoformat()
    referrer = _int_or_none(application.referrer_employee_ref)
    if referrer:
        payload["referrerEmployeeId"] = referrer
    if legacy is not None and _int_or_none(legacy.source_id):
        # Lets HRMS adopt an employee the legacy Recruitment page already linked, instead of
        # creating a second one for the same person.
        payload["legacyCandidateId"] = int(legacy.source_id)

    factory = client_factory or (
        lambda url, key: HrmsClient(url, key, timeout=settings.hrms_timeout_seconds)
    )
    try:
        data = factory(integration.hrms_base_url, api_key).create_hire(payload)
    except HandoffRejected as exc:
        existing.status = "failed"
        existing.last_error = f"rejected: {exc}"[:500]
        audit.record(db, organization_id=ctx.org.id, actor_user_id=ctx.user_id, action="handoff.rejected",
                     entity_type="application", entity_id=application.id)
        db.commit()
        raise HandoffUnavailable("hrms_rejected", str(exc), 422)
    except HandoffTransient as exc:
        existing.status = "failed"
        existing.last_error = f"retryable: {exc}"[:500]
        audit.record(db, organization_id=ctx.org.id, actor_user_id=ctx.user_id, action="handoff.transient_failure",
                     entity_type="application", entity_id=application.id)
        db.commit()
        raise HandoffUnavailable("hrms_unavailable", "HRMS did not confirm the hire. Safe to retry.", 502)

    existing.status = "completed"
    existing.employee_ref = str(data["employeeId"])
    existing.employee_code = (str(data["empId"])[:40] if data.get("empId") else None)
    existing.last_error = None
    existing.completed_at = utcnow()
    audit.record(db, organization_id=ctx.org.id, actor_user_id=ctx.user_id, action="handoff.completed",
                 entity_type="application", entity_id=application.id, created=bool(data.get("created")))
    db.commit()
    return existing
