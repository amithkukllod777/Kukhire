from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def new_id() -> str:
    return str(uuid4())


def utcnow() -> datetime:
    return datetime.utcnow()


class Organization(Base):
    """A recruiting workspace. Everything in KukHire hangs off one of these."""

    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    # The KukBook/HRMS company this workspace hands hires to. Set by the import / the admin who
    # links the workspace; unique so one company cannot be claimed by two workspaces.
    external_company_ref: Mapped[str | None] = mapped_column(String(64))
    created_by_user_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    jobs: Mapped[list["Job"]] = relationship(back_populates="organization")


class OrganizationMember(Base):
    """Workspace membership + role. This is KukHire's own authorization record: the shared
    Kuklabs Account proves WHO someone is, never which workspace they may open."""

    __tablename__ = "organization_members"
    __table_args__ = (UniqueConstraint("organization_id", "email", name="uq_member_org_email"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    # Kuklabs user id. NULL while an invitation has not been claimed by a verified account.
    user_id: Mapped[str | None] = mapped_column(String(64))
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)  # active | invited
    invited_by_user_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    department: Mapped[str | None] = mapped_column(String(100))
    location: Mapped[str | None] = mapped_column(String(200))
    employment_type: Mapped[str] = mapped_column(String(30), default="full_time", nullable=False)
    openings: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    requirements: Mapped[str | None] = mapped_column(Text)
    closing_date: Mapped[date | None] = mapped_column(Date)
    # Validated job-specific scoring criteria — see app/scoring.py. `scoring_weights` is the
    # earlier, unvalidated shape: still mapped so existing rows load, but nothing reads it.
    scoring_criteria: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    scoring_weights: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    organization: Mapped[Organization] = relationship(back_populates="jobs")
    applications: Mapped[list["Application"]] = relationship(back_populates="job")


class Candidate(Base):
    """A person, owned by exactly one organization. Applying to two jobs creates two
    Applications against ONE Candidate; the same person in two organizations is two Candidates."""

    __tablename__ = "candidates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # Nullable only because rows that predate tenant scoping and have no application cannot be
    # attributed to anyone (see migration 0002). The application layer never creates one.
    organization_id: Mapped[str | None] = mapped_column(ForeignKey("organizations.id"), index=True)
    full_name: Mapped[str] = mapped_column(String(180), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320), index=True)
    phone: Mapped[str | None] = mapped_column(String(40))
    source: Mapped[str | None] = mapped_column(String(50))
    resume_storage_key: Mapped[str | None] = mapped_column(String(500))
    resume_url: Mapped[str | None] = mapped_column(String(500))
    parsed_resume: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    applications: Mapped[list["Application"]] = relationship(back_populates="candidate")


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = ()

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str | None] = mapped_column(ForeignKey("organizations.id"), index=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidates.id"), index=True)
    stage: Mapped[str] = mapped_column(String(40), default="applied", nullable=False)
    source: Mapped[str | None] = mapped_column(String(50))
    # Referral ATTRIBUTION only. Whether a bonus is approved or paid is an HRMS/payroll decision.
    referrer_employee_ref: Mapped[str | None] = mapped_column(String(64))
    referrer_name: Mapped[str | None] = mapped_column(String(160))
    expected_salary: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    notice_period: Mapped[str | None] = mapped_column(String(50))
    rating: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)
    assigned_to: Mapped[str | None] = mapped_column(String(200))
    interview_date: Mapped[date | None] = mapped_column(Date)
    # Deprecated: kept because rows exist; the ingestion pipeline still records its source here.
    score: Mapped[float | None] = mapped_column(Float)
    evaluation: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    job: Mapped[Job] = relationship(back_populates="applications")
    candidate: Mapped[Candidate] = relationship(back_populates="applications")


class ApplicationStageHistory(Base):
    __tablename__ = "application_stage_history"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id"), index=True)
    from_stage: Mapped[str | None] = mapped_column(String(40))
    to_stage: Mapped[str] = mapped_column(String(40), nullable=False)
    changed_by_user_id: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Scorecard(Base):
    __tablename__ = "scorecards"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id"), index=True)
    interviewer_user_id: Mapped[str | None] = mapped_column(String(64))
    interviewer_name: Mapped[str | None] = mapped_column(String(160))
    overall: Mapped[Decimal] = mapped_column(Numeric(3, 1), nullable=False)
    technical: Mapped[Decimal | None] = mapped_column(Numeric(3, 1))
    culture: Mapped[Decimal | None] = mapped_column(Numeric(3, 1))
    notes: Mapped[str | None] = mapped_column(Text)
    interview_date: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class HiringHandoff(Base):
    """KukHire's record of a request to HRMS to turn an application into an employee. HRMS
    owns the employee; this row only remembers the reference HRMS returned."""

    __tablename__ = "hiring_handoffs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id"), unique=True)
    external_ref: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)  # pending|completed|failed
    employee_ref: Mapped[str | None] = mapped_column(String(64))
    employee_code: Mapped[str | None] = mapped_column(String(40))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(500))
    requested_by_user_id: Mapped[str | None] = mapped_column(String(64))
    start_date: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)


class OrganizationIntegration(Base):
    """One HRMS link per workspace: where to send hires and the (encrypted) company API key."""

    __tablename__ = "organization_integrations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), unique=True)
    hrms_base_url: Mapped[str] = mapped_column(String(300), nullable=False)
    encrypted_api_key: Mapped[str] = mapped_column(Text, nullable=False)
    api_key_hint: Mapped[str | None] = mapped_column(String(8))
    created_by_user_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class Evaluation(Base):
    """An ADVISORY AI evaluation. Never an input to a stage change."""

    __tablename__ = "evaluations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id"), index=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # completed | failed
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    criteria_snapshot: Mapped[list] = mapped_column(JSON, nullable=False)
    result: Mapped[dict | None] = mapped_column(JSON)
    overall: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    error: Mapped[str | None] = mapped_column(String(500))
    advisory: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str | None] = mapped_column(ForeignKey("organizations.id"), index=True)
    actor_user_id: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(40))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class LegacyImportMap(Base):
    __tablename__ = "legacy_import_map"
    __table_args__ = (
        UniqueConstraint("organization_id", "source_system", "source_table", "source_id", name="uq_legacy_import_source"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    source_system: Mapped[str] = mapped_column(String(40), nullable=False)
    source_company_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    source_table: Mapped[str] = mapped_column(String(60), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    target_table: Mapped[str] = mapped_column(String(60), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class LegacyImportQuarantine(Base):
    __tablename__ = "legacy_import_quarantine"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "source_system", "source_table", "source_id", name="uq_legacy_quarantine_source"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    source_system: Mapped[str] = mapped_column(String(40), nullable=False)
    source_table: Mapped[str] = mapped_column(String(60), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
