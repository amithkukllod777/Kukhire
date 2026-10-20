"""Import legacy KukBook recruitment data into KukHire.

Input: a bundle produced by KukBook's READ-ONLY exporter (``scripts/export-legacy-recruitment.cjs``).
This tool is run by an operator against a database; it is not an HTTP endpoint.

Properties (each is covered by tests/test_legacy_import.py):

* **Dry-run by default.** ``--apply`` is required to commit. A dry run executes the real import
  inside a transaction and rolls it back, so its numbers are exactly what ``--apply`` would do.
* **Idempotent.** Every imported source row is recorded in ``legacy_import_map`` (with a verbatim
  snapshot of the source row). Running again imports nothing twice.
* **Never fabricates a relationship.** A row that cannot be mapped honestly — unknown stage, missing
  parent job, a scorecard whose candidate does not exist — is NOT guessed into place. It goes to
  ``legacy_import_quarantine`` with the reason and the original data, and the report lists it.
* **Organization mapping is explicit.** The target workspace is named by the operator and must
  match the bundle's company; an unlinked workspace is only linked when ``--link-company`` is passed.
* **Nothing is dropped from the source.** This only reads the bundle; legacy tables stay as they are.
* **Reconciliation.** For every source table: source rows == imported + already imported +
  quarantined. The report says whether each balances, and the exit status is non-zero if not.

Scope notes: salary bands, referral bonus status and other fields with no home in KukHire are kept in
the mapping snapshot, not discarded. Referral *attribution* (who referred) is imported; approving or
paying a referral bonus remains an HRMS/payroll matter. Import is insert-once: correcting a
quarantined or already-imported row means fixing it in the source and importing again (new rows are
picked up) or editing it in KukHire — it does not overwrite existing KukHire rows.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db_models import (
    Application,
    ApplicationStageHistory,
    Candidate,
    HiringHandoff,
    Job,
    LegacyImportMap,
    LegacyImportQuarantine,
    Organization,
    OrganizationMember,
    Scorecard,
)
from app.pipeline import LEGACY_STAGE_MAP

SOURCE_SYSTEM = "kukbook-erp"
JOB_STATUSES = {"open", "on_hold", "closed"}
EMPLOYMENT_TYPES = {"full_time", "part_time", "contract", "internship"}
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class ImportError_(RuntimeError):
    """The import cannot proceed at all (bad bundle, wrong organization)."""


@dataclass
class TableReport:
    source: int = 0
    imported: int = 0
    already_imported: int = 0
    quarantined: int = 0
    warnings: Counter = field(default_factory=Counter)

    @property
    def balanced(self) -> bool:
        return self.source == self.imported + self.already_imported + self.quarantined


@dataclass
class ImportReport:
    dry_run: bool
    organization_id: str
    company_ref: str
    tables: dict[str, TableReport] = field(default_factory=dict)
    quarantine: list[dict] = field(default_factory=list)
    stage_totals: dict[str, dict[str, int]] = field(default_factory=dict)
    employee_links: dict[str, int] = field(default_factory=dict)

    @property
    def balanced(self) -> bool:
        return all(t.balanced for t in self.tables.values())

    def to_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "organization_id": self.organization_id,
            "company_ref": self.company_ref,
            "balanced": self.balanced,
            "tables": {k: {**{f: v for f, v in asdict(t).items() if f != "warnings"}, "warnings": dict(t.warnings), "balanced": t.balanced}
                       for k, t in self.tables.items()},
            "stage_totals": self.stage_totals,
            "employee_links": self.employee_links,
            "quarantine": self.quarantine,
        }


# --- parsing helpers ---------------------------------------------------------------------
def _text(value: Any) -> str | None:
    if value is None:
        return None
    v = str(value).strip()
    return v or None


def _date(value: Any) -> date | None:
    v = _text(value)
    if not v:
        return None
    try:
        return date.fromisoformat(v[:10])
    except ValueError:
        return None


def _timestamp(value: Any) -> datetime | None:
    v = _text(value)
    if not v:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(v[:19], fmt)
        except ValueError:
            continue
    return None


def _decimal(value: Any) -> Decimal | None:
    v = _text(value)
    if v is None:
        return None
    try:
        d = Decimal(v)
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _https_url(value: Any) -> str | None:
    from urllib.parse import urlsplit

    v = _text(value)
    if not v or len(v) > 500:
        return None
    parts = urlsplit(v)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return None
    return v


# --- bundle & organization -----------------------------------------------------------------
def load_bundle(path: str | Path) -> dict:
    bundle = json.loads(Path(path).read_text(encoding="utf-8"))
    if bundle.get("format") != "kukbook-recruitment-export" or bundle.get("format_version") != 1:
        raise ImportError_("This is not a kukbook-recruitment-export bundle (format/version mismatch).")
    if bundle.get("source_system") != SOURCE_SYSTEM:
        raise ImportError_(f"Unexpected source_system {bundle.get('source_system')!r}.")
    digest = hashlib.sha256(json.dumps(bundle["tables"], separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    # The exporter hashes JSON.stringify(tables) (compact, UTF-8); tolerate both key orders by
    # checking the hash it computed over the exact serialisation it used.
    if bundle.get("checksum") != digest:
        raise ImportError_("Bundle checksum does not match its contents; it was truncated or edited.")
    for name in ("job_positions", "candidates", "hr_interview_scorecards", "employees"):
        if not isinstance(bundle["tables"].get(name), list):
            raise ImportError_(f"Bundle is missing table {name}.")
    if not str((bundle.get("company") or {}).get("ref") or "").strip():
        raise ImportError_("Bundle has no company.ref.")
    return bundle


def resolve_organization(
    db: Session,
    bundle: dict,
    *,
    organization_id: str | None = None,
    create_name: str | None = None,
    admin_email: str | None = None,
    link_company: bool = False,
) -> Organization:
    ref = str(bundle["company"]["ref"])
    existing_for_ref = db.scalar(select(Organization).where(Organization.external_company_ref == ref))
    if create_name:
        if existing_for_ref is not None:
            raise ImportError_(f"Company {ref} is already linked to workspace {existing_for_ref.id}; import into it instead.")
        org = Organization(name=create_name, external_company_ref=ref)
        db.add(org)
        db.flush()
        if admin_email:
            db.add(OrganizationMember(organization_id=org.id, email=admin_email.strip().lower(), role="admin", status="invited"))
        return org
    if not organization_id:
        raise ImportError_("Name the target workspace (--organization-id) or create one (--create-organization).")
    org = db.get(Organization, organization_id)
    if org is None:
        raise ImportError_("That workspace does not exist.")
    if org.external_company_ref and org.external_company_ref != ref:
        raise ImportError_(f"Workspace is linked to company {org.external_company_ref}, but the bundle is for company {ref}.")
    if not org.external_company_ref:
        if existing_for_ref is not None:
            raise ImportError_(f"Company {ref} is already linked to a different workspace ({existing_for_ref.id}).")
        if not link_company:
            raise ImportError_("This workspace is not linked to a company yet; pass --link-company to link it to the bundle's company.")
        org.external_company_ref = ref
    return org


# --- the import ------------------------------------------------------------------------------
def run_import(db: Session, bundle: dict, org: Organization, *, dry_run: bool) -> ImportReport:
    """Run the import inside the caller's transaction. The caller commits or rolls back."""
    tables = bundle["tables"]
    ref = str(bundle["company"]["ref"])
    report = ImportReport(dry_run=dry_run, organization_id=org.id, company_ref=ref)
    for name in ("job_positions", "candidates", "hr_interview_scorecards"):
        report.tables[name] = TableReport(source=len(tables[name]))

    mapped: dict[tuple[str, str], str] = {
        (m.source_table, m.source_id): m.target_id
        for m in db.scalars(select(LegacyImportMap).where(
            LegacyImportMap.organization_id == org.id, LegacyImportMap.source_system == SOURCE_SYSTEM))
    }
    quarantined_existing = {
        (q.source_table, q.source_id): q
        for q in db.scalars(select(LegacyImportQuarantine).where(
            LegacyImportQuarantine.organization_id == org.id, LegacyImportQuarantine.source_system == SOURCE_SYSTEM))
    }
    employees = {str(e["id"]): e.get("empId") for e in tables["employees"]}

    def quarantine(table: str, source_id: str, reason: str, row: dict, *, blocking: bool = True) -> None:
        if blocking:
            report.tables[table.split(".")[0]].quarantined += 1
        report.quarantine.append({"table": table, "source_id": source_id, "reason": reason, "blocking": blocking})
        key = (table, source_id)
        if key not in quarantined_existing:
            q = LegacyImportQuarantine(organization_id=org.id, source_system=SOURCE_SYSTEM, source_table=table,
                                       source_id=source_id, reason=reason[:200], payload=row)
            db.add(q)
            quarantined_existing[key] = q

    def resolved(table: str, source_id: str) -> None:
        q = quarantined_existing.get((table, source_id))
        if q is not None and q.resolved_at is None:
            q.resolved_at = datetime.utcnow()

    def record(table: str, source_id: str, target_table: str, target_id: str, row: dict) -> None:
        db.add(LegacyImportMap(organization_id=org.id, source_system=SOURCE_SYSTEM, source_company_ref=ref,
                               source_table=table, source_id=source_id, target_table=target_table,
                               target_id=target_id, source_snapshot=row))
        mapped[(table, source_id)] = target_id

    # ---- jobs ----
    jobs = report.tables["job_positions"]
    for row in tables["job_positions"]:
        sid = str(row["jp_id"])
        if ("job_positions", sid) in mapped:
            jobs.already_imported += 1
            continue
        title = _text(row.get("jp_title"))
        status = (_text(row.get("jp_status")) or "open").lower()
        if not title:
            quarantine("job_positions", sid, "job has an empty title", row)
            continue
        if status not in JOB_STATUSES:
            quarantine("job_positions", sid, f"unknown job status {status!r}", row)
            continue
        etype = (_text(row.get("jp_type")) or "full_time").lower()
        if etype not in EMPLOYMENT_TYPES:
            jobs.warnings["employment_type_defaulted_to_full_time"] += 1
            etype = "full_time"
        closing = _date(row.get("jp_closingDate"))
        if _text(row.get("jp_closingDate")) and closing is None:
            jobs.warnings["closing_date_unparseable_dropped"] += 1
        try:
            openings = max(1, int(row.get("jp_openings") or 1))
        except (TypeError, ValueError):
            openings = 1
            jobs.warnings["openings_defaulted_to_1"] += 1
        created = _timestamp(row.get("jp_createdAt")) or datetime.utcnow()
        job = Job(organization_id=org.id, title=title[:180], description=_text(row.get("jp_description")) or "",
                  status=status, department=_text(row.get("jp_department")), location=_text(row.get("jp_location")),
                  employment_type=etype, openings=openings, requirements=_text(row.get("jp_requirements")),
                  closing_date=closing, scoring_criteria=[], created_at=created, updated_at=created)
        db.add(job)
        db.flush()
        record("job_positions", sid, "jobs", job.id, row)
        resolved("job_positions", sid)
        jobs.imported += 1

    # ---- candidates -> person + application (+ completed handoff when an employee link exists) ----
    cands = report.tables["candidates"]
    stage_source: Counter = Counter()
    stage_target: Counter = Counter()
    links_source = links_imported = 0
    for row in tables["candidates"]:
        sid = str(row["cand_id"])
        stage_source[(_text(row.get("cand_stage")) or "").lower()] += 1
        if row.get("cand_employeeId"):
            links_source += 1
        if ("candidates", sid) in mapped:
            cands.already_imported += 1
            app = db.get(Application, mapped[("candidates", sid)])
            if app is not None:
                stage_target[app.stage] += 1
                if row.get("cand_employeeId") and db.scalar(select(HiringHandoff.id).where(HiringHandoff.application_id == app.id)):
                    links_imported += 1
            continue
        job_target = mapped.get(("job_positions", str(row.get("cand_jobId"))))
        if job_target is None:
            quarantine("candidates", sid, "its job was not imported (missing or quarantined)", row)
            continue
        name = _text(row.get("cand_name"))
        if not name:
            quarantine("candidates", sid, "candidate has an empty name", row)
            continue
        stage = LEGACY_STAGE_MAP.get((_text(row.get("cand_stage")) or "").lower())
        if stage is None:
            quarantine("candidates", sid, f"unknown stage {row.get('cand_stage')!r}; not guessed", row)
            continue

        email = (_text(row.get("cand_email")) or "").lower() or None
        if email and not _EMAIL.match(email):
            cands.warnings["invalid_email_not_imported"] += 1
            email = None
        person = None
        if email:
            person = db.scalar(select(Candidate).where(Candidate.organization_id == org.id, func.lower(Candidate.email) == email))
        created = _timestamp(row.get("cand_createdAt")) or datetime.utcnow()
        if person is None:
            person = Candidate(organization_id=org.id, full_name=name[:180], email=email, phone=_text(row.get("cand_phone")),
                               source=_text(row.get("cand_source")), parsed_resume={}, created_at=created, updated_at=created)
            db.add(person)
            db.flush()
        elif person.full_name != name[:180]:
            cands.warnings["same_email_different_name_merged"] += 1

        if db.scalar(select(Application.id).where(Application.candidate_id == person.id, Application.job_id == job_target)):
            quarantine("candidates", sid, "duplicate application: the same person already applied to this job", row)
            continue

        resume = _text(row.get("cand_resumeUrl"))
        if resume:
            https = _https_url(resume)
            if https is None:
                # Keep the original reference for a person to look at; do not import it as a link.
                quarantine("candidates.resume_url", sid, "resume reference is not an https URL", {"cand_id": sid, "cand_resumeUrl": resume}, blocking=False)
                cands.warnings["resume_reference_quarantined"] += 1
            elif not person.resume_url:
                person.resume_url = https

        rating = row.get("cand_rating")
        try:
            rating = int(rating) if rating not in (None, "") else None
        except (TypeError, ValueError):
            rating = None
        if rating is not None and not 1 <= rating <= 5:
            cands.warnings["rating_out_of_range_dropped"] += 1
            rating = None
        salary = _decimal(row.get("cand_expectedSalary"))
        if salary is not None and salary < 0:
            salary = None
        referrer = row.get("cand_referrerEmployeeId")
        updated = _timestamp(row.get("cand_updatedAt")) or created
        app = Application(
            organization_id=org.id, job_id=job_target, candidate_id=person.id, stage=stage,
            source=_text(row.get("cand_source")), referrer_employee_ref=str(referrer) if referrer else None,
            expected_salary=salary, notice_period=_text(row.get("cand_noticePeriod")), rating=rating,
            notes=_text(row.get("cand_notes")), assigned_to=_text(row.get("cand_assignedTo")),
            interview_date=_date(row.get("cand_interviewDate")), evaluation={"source": "legacy_import"},
            created_at=created, updated_at=updated,
        )
        db.add(app)
        db.flush()
        db.add(ApplicationStageHistory(organization_id=org.id, application_id=app.id, from_stage=None, to_stage=stage,
                                       reason="imported from KukBook (legacy stage preserved)", created_at=created))
        if row.get("cand_employeeId"):
            emp = str(row["cand_employeeId"])
            if emp not in employees:
                cands.warnings["employee_link_kept_but_employee_not_in_export"] += 1
            db.add(HiringHandoff(
                organization_id=org.id, application_id=app.id, external_ref=f"legacy:{ref}:{sid}", status="completed",
                employee_ref=emp, employee_code=employees.get(emp), attempts=0, created_at=updated, completed_at=updated))
            links_imported += 1
        db.flush()
        record("candidates", sid, "applications", app.id, row)
        resolved("candidates", sid)
        stage_target[stage] += 1
        cands.imported += 1

    # ---- scorecards: only where the candidate link can be established ----
    cards = report.tables["hr_interview_scorecards"]
    for row in tables["hr_interview_scorecards"]:
        sid = str(row["his_id"])
        if ("hr_interview_scorecards", sid) in mapped:
            cards.already_imported += 1
            continue
        app_id = mapped.get(("candidates", str(row.get("his_candidateId"))))
        if app_id is None:
            quarantine("hr_interview_scorecards", sid, "no imported candidate for this scorecard; relationship not guessed", row)
            continue
        overall, technical, culture = _decimal(row.get("his_overall")), _decimal(row.get("his_technical")), _decimal(row.get("his_culture"))
        values = [v for v in (overall, technical, culture) if v is not None]
        if overall is None or any(not (Decimal(1) <= v <= Decimal(5)) for v in values):
            quarantine("hr_interview_scorecards", sid, "score outside the 1-5 scale; not coerced", row)
            continue
        created = _timestamp(row.get("his_createdAt")) or datetime.utcnow()
        card = Scorecard(organization_id=org.id, application_id=app_id, interviewer_user_id=None,
                         interviewer_name=_text(row.get("his_interviewerName")), overall=overall, technical=technical,
                         culture=culture, notes=_text(row.get("his_notes")), interview_date=_date(row.get("his_interviewDate")),
                         created_at=created)
        db.add(card)
        db.flush()
        record("hr_interview_scorecards", sid, "scorecards", card.id, row)
        resolved("hr_interview_scorecards", sid)
        cards.imported += 1

    report.stage_totals = {"source": dict(stage_source), "imported_as": dict(stage_target)}
    report.employee_links = {"source_rows_with_employee_link": links_source, "links_present_in_kukhire": links_imported}
    db.flush()
    return report


def format_report(report: ImportReport) -> str:
    lines = [f"{'DRY RUN (nothing committed)' if report.dry_run else 'APPLIED'} — workspace {report.organization_id}, company {report.company_ref}",
             "table                    source  imported  already  quarantined  balanced"]
    for name, t in report.tables.items():
        lines.append(f"{name:<24} {t.source:>6}  {t.imported:>8}  {t.already_imported:>7}  {t.quarantined:>11}  {'yes' if t.balanced else 'NO'}")
        for w, n in t.warnings.items():
            lines.append(f"    warning: {w} x{n}")
    lines.append(f"stages — source: {report.stage_totals.get('source')}  imported as: {report.stage_totals.get('imported_as')}")
    lines.append(f"employee links — {report.employee_links}")
    for q in report.quarantine:
        lines.append(f"  QUARANTINE {q['table']}#{q['source_id']}: {q['reason']}")
    lines.append("RECONCILED" if report.balanced else "NOT RECONCILED — investigate before relying on this import")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from app.config import get_settings
    from app.database import build_engine, build_session_factory
    from app import migrations

    p = argparse.ArgumentParser(description="Import a KukBook recruitment export into KukHire (dry-run unless --apply).")
    p.add_argument("--bundle", required=True)
    p.add_argument("--organization-id")
    p.add_argument("--create-organization", metavar="NAME")
    p.add_argument("--admin-email", help="with --create-organization: invite this email as the workspace admin")
    p.add_argument("--link-company", action="store_true", help="link an unlinked workspace to the bundle's company")
    p.add_argument("--apply", action="store_true", help="commit. Without it this is a dry run that rolls back.")
    p.add_argument("--report", help="write the full JSON report here")
    p.add_argument("--database-url", help="defaults to DATABASE_URL / KUKHIRE_DATABASE_URL")
    args = p.parse_args(argv)

    try:
        bundle = load_bundle(args.bundle)
        engine = build_engine(args.database_url or get_settings().database_url)
        pending = migrations.pending(engine)
        if pending:
            raise ImportError_(f"Database has pending migrations ({', '.join(m.version for m in pending)}); run them first.")
        with build_session_factory(engine)() as db:
            org = resolve_organization(db, bundle, organization_id=args.organization_id, create_name=args.create_organization,
                                       admin_email=args.admin_email, link_company=args.link_company)
            report = run_import(db, bundle, org, dry_run=not args.apply)
            if args.apply and report.balanced:
                db.commit()
            else:
                db.rollback()
    except ImportError_ as exc:
        print(f"Import refused: {exc}", file=sys.stderr)
        return 1
    print(format_report(report))
    if args.report:
        Path(args.report).write_text(json.dumps(report.to_dict(), indent=2, default=str), encoding="utf-8")
    if args.apply and not report.balanced:
        print("Nothing was committed because the reconciliation does not balance.", file=sys.stderr)
    return 0 if report.balanced else 2


if __name__ == "__main__":
    sys.exit(main())
