"""Tenant-scoped ATS schema (upgrade from the original core schema).

Starting point: 0000/0001 — organizations, jobs, candidates, applications, mailbox tables, with
*global* candidate-email uniqueness (one candidate row could be shared by every organization).

This migration:
  1. adds the organization/workspace tables (members + roles), stage history, scorecards, hiring
     handoffs, per-org HRMS link, advisory evaluations, audit log, and the legacy-import mapping
     and quarantine tables;
  2. adds the missing ATS columns to jobs / candidates / applications / ingested_emails;
  3. makes candidates organization-owned. Rows are assigned ONLY where the existing relationships
     prove an owner:
       * a candidate whose applications all sit in one organization -> that organization;
       * a candidate shared across organizations -> one copy per organization, applications
         repointed, so no tenant can see another tenant's candidate;
       * a candidate with no applications -> left with organization_id NULL. It is NOT guessed
         into any organization; it stays in the table, invisible to every tenant, and is
         counted in the migration output for review. Nothing is deleted;
  4. replaces the global unique index on candidate email with a per-organization one.

Idempotent: every step checks what already exists. Portable across SQLite (local development) and
PostgreSQL (production). Nothing is dropped except the global unique index it replaces.
"""
from __future__ import annotations

import json
import uuid

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    inspect,
    text,
)
from sqlalchemy.engine import Connection


def _columns(conn: Connection, table: str) -> set[str]:
    return {c["name"] for c in inspect(conn).get_columns(table)}


def _add_column(conn: Connection, table: str, name: str, column_type, *, extra: str = "") -> bool:
    if name in _columns(conn, table):
        return False
    rendered = column_type.compile(dialect=conn.dialect)
    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {rendered} {extra}".strip()))
    return True


def _new_tables(conn: Connection) -> None:
    md = MetaData()
    # The new tables reference these; reflect them (they exist by now) so the foreign keys resolve.
    # create_all(checkfirst=True) below then skips them because they already exist.
    for existing in ("organizations", "jobs", "applications"):
        Table(existing, md, autoload_with=conn)
    now = func.current_timestamp()

    Table(
        "organization_members", md,
        Column("id", String(36), primary_key=True),
        Column("organization_id", String(36), ForeignKey("organizations.id"), nullable=False, index=True),
        Column("user_id", String(64), nullable=True),
        Column("email", String(320), nullable=False),
        Column("role", String(20), nullable=False),
        Column("status", String(20), nullable=False, server_default="active"),
        Column("invited_by_user_id", String(64), nullable=True),
        Column("created_at", DateTime, nullable=False, server_default=now),
        UniqueConstraint("organization_id", "email", name="uq_member_org_email"),
    )
    Table(
        "application_stage_history", md,
        Column("id", String(36), primary_key=True),
        Column("organization_id", String(36), ForeignKey("organizations.id"), nullable=False, index=True),
        Column("application_id", String(36), ForeignKey("applications.id"), nullable=False, index=True),
        Column("from_stage", String(40), nullable=True),
        Column("to_stage", String(40), nullable=False),
        Column("changed_by_user_id", String(64), nullable=True),
        Column("reason", String(500), nullable=True),
        Column("created_at", DateTime, nullable=False, server_default=now),
    )
    Table(
        "scorecards", md,
        Column("id", String(36), primary_key=True),
        Column("organization_id", String(36), ForeignKey("organizations.id"), nullable=False, index=True),
        Column("application_id", String(36), ForeignKey("applications.id"), nullable=False, index=True),
        Column("interviewer_user_id", String(64), nullable=True),
        Column("interviewer_name", String(160), nullable=True),
        Column("overall", Numeric(3, 1), nullable=False),
        Column("technical", Numeric(3, 1), nullable=True),
        Column("culture", Numeric(3, 1), nullable=True),
        Column("notes", Text, nullable=True),
        Column("interview_date", Date, nullable=True),
        Column("created_at", DateTime, nullable=False, server_default=now),
    )
    Table(
        "hiring_handoffs", md,
        Column("id", String(36), primary_key=True),
        Column("organization_id", String(36), ForeignKey("organizations.id"), nullable=False, index=True),
        Column("application_id", String(36), ForeignKey("applications.id"), nullable=False, unique=True),
        # Stable key sent to HRMS. Never reused, never derived from mutable data.
        Column("external_ref", String(160), nullable=False, unique=True),
        Column("status", String(20), nullable=False, server_default="pending"),
        Column("employee_ref", String(64), nullable=True),
        Column("employee_code", String(40), nullable=True),
        Column("attempts", Integer, nullable=False, server_default="0"),
        Column("last_error", String(500), nullable=True),
        Column("requested_by_user_id", String(64), nullable=True),
        Column("start_date", Date, nullable=True),
        Column("created_at", DateTime, nullable=False, server_default=now),
        Column("updated_at", DateTime, nullable=False, server_default=now),
        Column("completed_at", DateTime, nullable=True),
    )
    Table(
        "organization_integrations", md,
        Column("id", String(36), primary_key=True),
        Column("organization_id", String(36), ForeignKey("organizations.id"), nullable=False, unique=True),
        Column("hrms_base_url", String(300), nullable=False),
        Column("encrypted_api_key", Text, nullable=False),
        Column("api_key_hint", String(8), nullable=True),
        Column("created_by_user_id", String(64), nullable=True),
        Column("created_at", DateTime, nullable=False, server_default=now),
        Column("updated_at", DateTime, nullable=False, server_default=now),
    )
    Table(
        "evaluations", md,
        Column("id", String(36), primary_key=True),
        Column("organization_id", String(36), ForeignKey("organizations.id"), nullable=False, index=True),
        Column("application_id", String(36), ForeignKey("applications.id"), nullable=False, index=True),
        Column("job_id", String(36), ForeignKey("jobs.id"), nullable=False),
        Column("status", String(20), nullable=False),
        Column("provider", String(40), nullable=False),
        Column("model", String(120), nullable=False),
        Column("criteria_snapshot", JSON, nullable=False),
        Column("result", JSON, nullable=True),
        Column("overall", Numeric(5, 2), nullable=True),
        Column("error", String(500), nullable=True),
        Column("advisory", Boolean, nullable=False, server_default="1" if conn.dialect.name == "sqlite" else "true"),
        Column("created_by_user_id", String(64), nullable=True),
        Column("created_at", DateTime, nullable=False, server_default=now),
    )
    Table(
        "audit_log", md,
        Column("id", String(36), primary_key=True),
        Column("organization_id", String(36), ForeignKey("organizations.id"), nullable=True, index=True),
        Column("actor_user_id", String(64), nullable=True),
        Column("action", String(80), nullable=False),
        Column("entity_type", String(40), nullable=True),
        Column("entity_id", String(64), nullable=True),
        # Identifiers and field NAMES only. Never candidate content, never resume text.
        Column("detail", JSON, nullable=False),
        Column("created_at", DateTime, nullable=False, server_default=now),
    )
    Table(
        "legacy_import_map", md,
        Column("id", String(36), primary_key=True),
        Column("organization_id", String(36), ForeignKey("organizations.id"), nullable=False, index=True),
        Column("source_system", String(40), nullable=False),
        Column("source_company_ref", String(64), nullable=False),
        Column("source_table", String(60), nullable=False),
        Column("source_id", String(64), nullable=False),
        Column("target_table", String(60), nullable=False),
        Column("target_id", String(36), nullable=False),
        # The complete source row, verbatim, so fields with no home in this schema (salary band,
        # referral bonus status, legacy timestamps) are preserved rather than silently dropped.
        Column("source_snapshot", JSON, nullable=True),
        Column("created_at", DateTime, nullable=False, server_default=now),
        UniqueConstraint(
            "organization_id", "source_system", "source_table", "source_id", name="uq_legacy_import_source"
        ),
    )
    Table(
        "legacy_import_quarantine", md,
        Column("id", String(36), primary_key=True),
        Column("organization_id", String(36), ForeignKey("organizations.id"), nullable=False, index=True),
        Column("source_system", String(40), nullable=False),
        Column("source_table", String(60), nullable=False),
        Column("source_id", String(64), nullable=False),
        Column("reason", String(200), nullable=False),
        Column("payload", JSON, nullable=False),
        Column("created_at", DateTime, nullable=False, server_default=now),
        Column("resolved_at", DateTime, nullable=True),
        UniqueConstraint(
            "organization_id", "source_system", "source_table", "source_id", name="uq_legacy_quarantine_source"
        ),
    )
    md.create_all(conn, checkfirst=True)


def _widen_existing(conn: Connection) -> None:
    _add_column(conn, "organizations", "external_company_ref", String(64))
    _add_column(conn, "organizations", "created_by_user_id", String(64))

    _add_column(conn, "jobs", "department", String(100))
    _add_column(conn, "jobs", "location", String(200))
    _add_column(conn, "jobs", "employment_type", String(30), extra="NOT NULL DEFAULT 'full_time'")
    _add_column(conn, "jobs", "openings", Integer(), extra="NOT NULL DEFAULT 1")
    _add_column(conn, "jobs", "requirements", Text())
    _add_column(conn, "jobs", "closing_date", Date())
    # Validated job-specific scoring criteria (list of {key,label,weight,description}).
    # `scoring_weights` stays for history; nothing reads it any more.
    _add_column(conn, "jobs", "scoring_criteria", JSON(), extra="NOT NULL DEFAULT '[]'")
    _add_column(conn, "jobs", "created_by_user_id", String(64))
    _add_column(conn, "jobs", "updated_at", DateTime())

    _add_column(conn, "candidates", "organization_id", String(36), extra="REFERENCES organizations(id)")
    _add_column(conn, "candidates", "source", String(50))
    # A reference to a resume stored elsewhere (https only). KukHire does not host the file.
    _add_column(conn, "candidates", "resume_url", String(500))
    _add_column(conn, "candidates", "created_by_user_id", String(64))
    _add_column(conn, "candidates", "updated_at", DateTime())

    _add_column(conn, "applications", "organization_id", String(36), extra="REFERENCES organizations(id)")
    _add_column(conn, "applications", "source", String(50))
    _add_column(conn, "applications", "referrer_employee_ref", String(64))
    _add_column(conn, "applications", "referrer_name", String(160))
    _add_column(conn, "applications", "expected_salary", Numeric(15, 2))
    _add_column(conn, "applications", "notice_period", String(50))
    _add_column(conn, "applications", "rating", Integer())
    _add_column(conn, "applications", "notes", Text())
    _add_column(conn, "applications", "assigned_to", String(200))
    _add_column(conn, "applications", "interview_date", Date())
    _add_column(conn, "applications", "created_by_user_id", String(64))
    _add_column(conn, "applications", "updated_at", DateTime())

    if "ingested_emails" in inspect(conn).get_table_names():
        _add_column(conn, "ingested_emails", "organization_id", String(36), extra="REFERENCES organizations(id)")


def _assign_tenants(conn: Connection) -> dict[str, int]:
    """Give every row an owner organization — only where the data proves one."""
    stats = {"applications_assigned": 0, "candidates_assigned": 0, "candidates_split": 0, "candidates_unowned": 0}

    result = conn.execute(
        text(
            "UPDATE applications SET organization_id = "
            "(SELECT j.organization_id FROM jobs j WHERE j.id = applications.job_id) "
            "WHERE organization_id IS NULL"
        )
    )
    stats["applications_assigned"] = max(result.rowcount or 0, 0)

    # Candidates that were shared across organizations: one copy per organization.
    shared = conn.execute(
        text(
            "SELECT candidate_id FROM applications WHERE organization_id IS NOT NULL "
            "GROUP BY candidate_id HAVING COUNT(DISTINCT organization_id) > 1"
        )
    ).all()
    for (candidate_id,) in shared:
        orgs = [
            r[0]
            for r in conn.execute(
                text(
                    "SELECT DISTINCT organization_id FROM applications "
                    "WHERE candidate_id = :c AND organization_id IS NOT NULL ORDER BY organization_id"
                ),
                {"c": candidate_id},
            ).all()
        ]
        original = conn.execute(
            text(
                "SELECT full_name, email, phone, resume_storage_key, parsed_resume, created_at "
                "FROM candidates WHERE id = :c"
            ),
            {"c": candidate_id},
        ).one()
        # The first organization keeps the original row; every other one gets its own copy.
        conn.execute(
            text("UPDATE candidates SET organization_id = :o WHERE id = :c AND organization_id IS NULL"),
            {"o": orgs[0], "c": candidate_id},
        )
        for org in orgs[1:]:
            copy_id = str(uuid.uuid4())
            conn.execute(
                text(
                    "INSERT INTO candidates (id, full_name, email, phone, resume_storage_key, parsed_resume, "
                    "created_at, organization_id) VALUES (:id, :n, :e, :p, :r, :pr, :ts, :o)"
                ),
                {
                    "id": copy_id, "n": original[0], "e": original[1], "p": original[2], "r": original[3],
                    "pr": original[4] if isinstance(original[4], str) else json.dumps(original[4]),
                    "ts": original[5], "o": org,
                },
            )
            conn.execute(
                text(
                    "UPDATE applications SET candidate_id = :new WHERE candidate_id = :old AND organization_id = :o"
                ),
                {"new": copy_id, "old": candidate_id, "o": org},
            )
            stats["candidates_split"] += 1

    result = conn.execute(
        text(
            "UPDATE candidates SET organization_id = "
            "(SELECT MIN(a.organization_id) FROM applications a WHERE a.candidate_id = candidates.id) "
            "WHERE organization_id IS NULL AND EXISTS "
            "(SELECT 1 FROM applications a WHERE a.candidate_id = candidates.id)"
        )
    )
    stats["candidates_assigned"] = max(result.rowcount or 0, 0)
    stats["candidates_unowned"] = int(
        conn.execute(text("SELECT COUNT(*) FROM candidates WHERE organization_id IS NULL")).scalar() or 0
    )

    if "ingested_emails" in inspect(conn).get_table_names():
        conn.execute(
            text(
                "UPDATE ingested_emails SET organization_id = "
                "(SELECT j.organization_id FROM jobs j WHERE j.id = ingested_emails.job_id) "
                "WHERE organization_id IS NULL AND job_id IS NOT NULL"
            )
        )
        conn.execute(
            text(
                "UPDATE ingested_emails SET organization_id = "
                "(SELECT m.organization_id FROM mailbox_connections m WHERE m.id = ingested_emails.mailbox_id) "
                "WHERE organization_id IS NULL"
            )
        )
    return stats


def _drop_global_email_uniqueness(conn: Connection) -> None:
    # The old index made ONE candidate row per email across every organization. It must go BEFORE
    # tenant assignment, because splitting a shared candidate inserts a second row with that email.
    conn.execute(text("DROP INDEX IF EXISTS uq_candidate_email_normalized"))


def _indexes(conn: Connection) -> None:
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_candidate_org_email "
            "ON candidates (organization_id, LOWER(email)) "
            "WHERE email IS NOT NULL AND organization_id IS NOT NULL"
        )
    )
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_candidates_organization_id ON candidates (organization_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_applications_organization_id ON applications (organization_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_applications_stage ON applications (organization_id, stage)"))
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_member_org_user ON organization_members (organization_id, user_id) "
            "WHERE user_id IS NOT NULL"
        )
    )
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_org_external_company ON organizations (external_company_ref) "
            "WHERE external_company_ref IS NOT NULL"
        )
    )
    if "ingested_emails" in inspect(conn).get_table_names():
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_ingested_emails_organization_id ON ingested_emails (organization_id)")
        )


def upgrade(conn: Connection) -> None:
    _widen_existing(conn)
    _new_tables(conn)
    _drop_global_email_uniqueness(conn)
    stats = _assign_tenants(conn)
    _indexes(conn)
    print(f"[0002] tenant assignment: {stats}")
