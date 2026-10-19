"""Jobs, candidates, applications, the pipeline, scorecards, handoff and evaluations.

Every query filters by ``ctx.org.id`` — the organization the caller was verified to belong to —
and every object looked up by id is fetched WITH that filter, so an id from another workspace is
indistinguishable from an id that does not exist (404).
"""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit, evaluation as evaluation_service, handoff as handoff_service
from app.database import get_db
from app.db_models import (
    Application,
    ApplicationStageHistory,
    Candidate,
    Evaluation,
    HiringHandoff,
    Job,
    LegacyImportMap,
    Scorecard,
    utcnow,
)
from app.pipeline import OPEN_STAGES, STAGES, TransitionError, validate_transition
from app.schemas import (
    ApplicationCreate,
    ApplicationPatch,
    CandidateIn,
    CandidatePatch,
    HandoffIn,
    JobIn,
    JobPatch,
    ScorecardIn,
    TransitionIn,
)
from app.scoring import CriteriaError, validate_criteria
from app.tenancy import OrgContext, can, require

router = APIRouter(prefix="/v1/organizations/{org_id}", tags=["ats"])


def _err(http_status: int, code: str, message: str, **extra) -> HTTPException:
    return HTTPException(http_status, detail={"error": code, "message": message, **extra})


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped.lower()}%"


def _num(value: Decimal | float | None) -> float | None:
    return None if value is None else float(value)


# --- serializers -----------------------------------------------------------------------------
def job_out(job: Job, applications_count: int | None = None) -> dict:
    out = {
        "id": job.id, "title": job.title, "description": job.description, "status": job.status,
        "department": job.department, "location": job.location, "employment_type": job.employment_type,
        "openings": job.openings, "requirements": job.requirements,
        "closing_date": job.closing_date.isoformat() if job.closing_date else None,
        "scoring_criteria": job.scoring_criteria or [],
        "created_at": job.created_at.isoformat() if job.created_at else None,
    }
    if applications_count is not None:
        out["applications_count"] = applications_count
    return out


def candidate_out(c: Candidate) -> dict:
    return {
        "id": c.id, "full_name": c.full_name, "email": c.email, "phone": c.phone, "source": c.source,
        "resume_url": c.resume_url, "has_parsed_resume": bool(c.parsed_resume),
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def application_out(a: Application, c: Candidate, j: Job) -> dict:
    return {
        "id": a.id, "stage": a.stage, "job_id": j.id, "job_title": j.title,
        "candidate_id": c.id, "candidate_name": c.full_name, "candidate_email": c.email,
        "source": a.source, "rating": a.rating, "assigned_to": a.assigned_to,
        "interview_date": a.interview_date.isoformat() if a.interview_date else None,
        "referrer_employee_ref": a.referrer_employee_ref, "referrer_name": a.referrer_name,
        "expected_salary": _num(a.expected_salary), "notice_period": a.notice_period, "notes": a.notes,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
    }


def scorecard_out(s: Scorecard) -> dict:
    return {
        "id": s.id, "application_id": s.application_id, "interviewer_name": s.interviewer_name,
        "overall": _num(s.overall), "technical": _num(s.technical), "culture": _num(s.culture),
        "notes": s.notes, "interview_date": s.interview_date.isoformat() if s.interview_date else None,
        "mine": False, "created_at": s.created_at.isoformat() if s.created_at else None,
    }


# --- scoped lookups --------------------------------------------------------------------------
def _job(db: Session, ctx: OrgContext, job_id: str) -> Job:
    job = db.scalar(select(Job).where(Job.id == job_id, Job.organization_id == ctx.org.id))
    if job is None:
        raise _err(404, "not_found", "Job not found.")
    return job


def _candidate(db: Session, ctx: OrgContext, candidate_id: str) -> Candidate:
    c = db.scalar(select(Candidate).where(Candidate.id == candidate_id, Candidate.organization_id == ctx.org.id))
    if c is None:
        raise _err(404, "not_found", "Candidate not found.")
    return c


def _application(db: Session, ctx: OrgContext, application_id: str) -> Application:
    a = db.scalar(select(Application).where(Application.id == application_id, Application.organization_id == ctx.org.id))
    if a is None:
        raise _err(404, "not_found", "Application not found.")
    return a


# --- jobs ------------------------------------------------------------------------------------
@router.get("/jobs")
def list_jobs(
    status: str | None = None,
    q: str | None = Query(default=None, max_length=100),
    ctx: OrgContext = Depends(require("jobs.read")),
    db: Session = Depends(get_db),
):
    stmt = select(Job).where(Job.organization_id == ctx.org.id)
    if status:
        stmt = stmt.where(Job.status == status)
    if q:
        stmt = stmt.where(func.lower(Job.title).like(_like(q), escape="\\"))
    jobs = db.scalars(stmt.order_by(Job.created_at.desc()).limit(200)).all()
    counts = dict(
        db.execute(
            select(Application.job_id, func.count()).where(Application.organization_id == ctx.org.id).group_by(Application.job_id)
        ).all()
    )
    return {"items": [job_out(j, counts.get(j.id, 0)) for j in jobs]}


@router.post("/jobs", status_code=201)
def create_job(body: JobIn, ctx: OrgContext = Depends(require("jobs.write")), db: Session = Depends(get_db)):
    try:
        criteria = validate_criteria(body.scoring_criteria) if body.scoring_criteria else []
    except CriteriaError as exc:
        raise _err(422, "invalid_criteria", str(exc))
    job = Job(
        organization_id=ctx.org.id, title=body.title, description=body.description, status=body.status,
        department=body.department, location=body.location, employment_type=body.employment_type,
        openings=body.openings, requirements=body.requirements, closing_date=body.closing_date,
        scoring_criteria=criteria, created_by_user_id=ctx.user_id,
    )
    db.add(job)
    db.flush()
    audit.record(db, organization_id=ctx.org.id, actor_user_id=ctx.user_id, action="job.created",
                 entity_type="job", entity_id=job.id)
    db.commit()
    return job_out(job, 0)


@router.get("/jobs/{job_id}")
def get_job(job_id: str, ctx: OrgContext = Depends(require("jobs.read")), db: Session = Depends(get_db)):
    job = _job(db, ctx, job_id)
    count = db.scalar(select(func.count()).select_from(Application).where(Application.job_id == job.id)) or 0
    return job_out(job, int(count))


@router.patch("/jobs/{job_id}")
def update_job(job_id: str, body: JobPatch, ctx: OrgContext = Depends(require("jobs.write")), db: Session = Depends(get_db)):
    job = _job(db, ctx, job_id)
    changes = body.model_dump(exclude_unset=True)
    if "scoring_criteria" in changes:
        try:
            changes["scoring_criteria"] = validate_criteria(changes["scoring_criteria"]) if changes["scoring_criteria"] else []
        except CriteriaError as exc:
            raise _err(422, "invalid_criteria", str(exc))
    for field, value in changes.items():
        setattr(job, field, value)
    audit.record(db, organization_id=ctx.org.id, actor_user_id=ctx.user_id, action="job.updated",
                 entity_type="job", entity_id=job.id, fields=sorted(changes))
    db.commit()
    return job_out(job)


@router.delete("/jobs/{job_id}", status_code=204)
def delete_job(job_id: str, ctx: OrgContext = Depends(require("org.manage")), db: Session = Depends(get_db)):
    job = _job(db, ctx, job_id)
    count = db.scalar(select(func.count()).select_from(Application).where(Application.job_id == job.id)) or 0
    if count:
        # The legacy page cascade-deleted every candidate of a job. Hiring history is not
        # something to lose to a mis-click: close the job instead.
        raise _err(409, "job_has_applications", "This job has applications. Close it instead of deleting it.")
    audit.record(db, organization_id=ctx.org.id, actor_user_id=ctx.user_id, action="job.deleted",
                 entity_type="job", entity_id=job.id)
    db.delete(job)
    db.commit()


# --- candidates ------------------------------------------------------------------------------
@router.get("/candidates")
def list_candidates(
    q: str | None = Query(default=None, max_length=100),
    source: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    ctx: OrgContext = Depends(require("candidates.read")),
    db: Session = Depends(get_db),
):
    stmt = select(Candidate).where(Candidate.organization_id == ctx.org.id)
    if source:
        stmt = stmt.where(Candidate.source == source)
    if q:
        term = _like(q)
        stmt = stmt.where(or_(func.lower(Candidate.full_name).like(term, escape="\\"), func.lower(Candidate.email).like(term, escape="\\")))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(Candidate.created_at.desc()).limit(limit).offset(offset)).all()
    return {"items": [candidate_out(c) for c in rows], "total": int(total)}


def _find_by_email(db: Session, org_id: str, email: str | None) -> Candidate | None:
    if not email:
        return None
    return db.scalar(
        select(Candidate).where(Candidate.organization_id == org_id, func.lower(Candidate.email) == email.strip().lower())
    )


def _new_candidate(ctx: OrgContext, body: CandidateIn) -> Candidate:
    return Candidate(
        organization_id=ctx.org.id, full_name=body.full_name,
        email=str(body.email).strip().lower() if body.email else None, phone=body.phone,
        source=body.source, resume_url=body.resume_url, parsed_resume=body.parsed_resume or {},
        created_by_user_id=ctx.user_id,
    )


@router.post("/candidates", status_code=201)
def create_candidate(body: CandidateIn, ctx: OrgContext = Depends(require("candidates.write")), db: Session = Depends(get_db)):
    existing = _find_by_email(db, ctx.org.id, str(body.email) if body.email else None)
    if existing is not None:
        raise _err(409, "duplicate_candidate", "A candidate with this email already exists.", candidate_id=existing.id)
    c = _new_candidate(ctx, body)
    db.add(c)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = _find_by_email(db, ctx.org.id, str(body.email) if body.email else None)
        raise _err(409, "duplicate_candidate", "A candidate with this email already exists.",
                   candidate_id=existing.id if existing else None)
    audit.record(db, organization_id=ctx.org.id, actor_user_id=ctx.user_id, action="candidate.created",
                 entity_type="candidate", entity_id=c.id)
    db.commit()
    return candidate_out(c)


@router.get("/candidates/{candidate_id}")
def get_candidate(candidate_id: str, ctx: OrgContext = Depends(require("candidates.read")), db: Session = Depends(get_db)):
    c = _candidate(db, ctx, candidate_id)
    apps = db.execute(
        select(Application, Job).join(Job, Job.id == Application.job_id)
        .where(Application.candidate_id == c.id, Application.organization_id == ctx.org.id)
        .order_by(Application.created_at.desc())
    ).all()
    return {**candidate_out(c), "applications": [application_out(a, c, j) for a, j in apps]}


@router.patch("/candidates/{candidate_id}")
def update_candidate(
    candidate_id: str, body: CandidatePatch, ctx: OrgContext = Depends(require("candidates.write")), db: Session = Depends(get_db)
):
    c = _candidate(db, ctx, candidate_id)
    changes = body.model_dump(exclude_unset=True)
    if "email" in changes and changes["email"]:
        changes["email"] = str(changes["email"]).strip().lower()
    for field, value in changes.items():
        setattr(c, field, value)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise _err(409, "duplicate_candidate", "Another candidate in this workspace already has that email.")
    audit.record(db, organization_id=ctx.org.id, actor_user_id=ctx.user_id, action="candidate.updated",
                 entity_type="candidate", entity_id=c.id, fields=sorted(changes))
    db.commit()
    return candidate_out(c)


@router.delete("/candidates/{candidate_id}", status_code=204)
def erase_candidate(candidate_id: str, ctx: OrgContext = Depends(require("candidates.erase")), db: Session = Depends(get_db)):
    """Right-to-erasure: removes the person and everything hanging off them in this workspace."""
    c = _candidate(db, ctx, candidate_id)
    app_ids = [a for (a,) in db.execute(select(Application.id).where(Application.candidate_id == c.id)).all()]
    if app_ids and db.scalar(
        select(func.count()).select_from(HiringHandoff).where(HiringHandoff.application_id.in_(app_ids), HiringHandoff.status == "completed")
    ):
        # A completed handoff means HRMS holds an employee record. That is an HR record, not a
        # recruitment one, and is erased (or retained) under HRMS's rules — not by this endpoint.
        raise _err(409, "hired_in_hrms", "This person was hired and handed to HRMS; their employee record is managed there.")
    if app_ids:
        scorecard_ids = [i for (i,) in db.execute(select(Scorecard.id).where(Scorecard.application_id.in_(app_ids))).all()]
        # The import mapping keeps a verbatim copy of each legacy source row (personal data), so it
        # goes with the person: applications' and scorecards' mappings alike.
        db.execute(delete(LegacyImportMap).where(LegacyImportMap.target_table == "applications", LegacyImportMap.target_id.in_(app_ids)))
        if scorecard_ids:
            db.execute(delete(LegacyImportMap).where(LegacyImportMap.target_table == "scorecards", LegacyImportMap.target_id.in_(scorecard_ids)))
        for model in (Scorecard, Evaluation, ApplicationStageHistory, HiringHandoff):
            db.execute(delete(model).where(model.application_id.in_(app_ids)))
        db.execute(delete(Application).where(Application.id.in_(app_ids)))
    audit.record(db, organization_id=ctx.org.id, actor_user_id=ctx.user_id, action="candidate.erased",
                 entity_type="candidate", entity_id=c.id, applications=len(app_ids))
    db.delete(c)
    db.commit()


# --- applications ----------------------------------------------------------------------------
@router.post("/applications", status_code=201)
def create_application(body: ApplicationCreate, ctx: OrgContext = Depends(require("applications.write")), db: Session = Depends(get_db)):
    job = _job(db, ctx, body.job_id)
    if job.status == "closed":
        raise _err(409, "job_closed", "This job is closed to new applications.")
    if body.candidate_id:
        candidate = _candidate(db, ctx, body.candidate_id)
    else:
        # Identity resolution: a normalized email within THIS workspace. Never by name — two
        # different people share names. No email means a new person every time.
        candidate = _find_by_email(db, ctx.org.id, str(body.candidate.email) if body.candidate.email else None)
        if candidate is None:
            candidate = _new_candidate(ctx, body.candidate)
            db.add(candidate)
            db.flush()
    app = Application(
        organization_id=ctx.org.id, job_id=job.id, candidate_id=candidate.id, stage="applied",
        source=body.source or (body.candidate.source if body.candidate else None),
        referrer_employee_ref=body.referrer_employee_ref, referrer_name=body.referrer_name,
        expected_salary=body.expected_salary, notice_period=body.notice_period, notes=body.notes,
        created_by_user_id=ctx.user_id,
    )
    db.add(app)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(Application).where(Application.candidate_id == candidate.id, Application.job_id == job.id)
        )
        raise _err(409, "already_applied", "This candidate already has an application for this job.",
                   application_id=existing.id if existing else None)
    db.add(ApplicationStageHistory(
        organization_id=ctx.org.id, application_id=app.id, from_stage=None, to_stage="applied",
        changed_by_user_id=ctx.user_id, reason="application created",
    ))
    audit.record(db, organization_id=ctx.org.id, actor_user_id=ctx.user_id, action="application.created",
                 entity_type="application", entity_id=app.id)
    db.commit()
    return application_out(app, candidate, job)


@router.get("/applications")
def list_applications(
    job_id: str | None = None,
    stage: str | None = None,
    q: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: OrgContext = Depends(require("applications.read")),
    db: Session = Depends(get_db),
):
    if stage and stage not in STAGES:
        raise _err(422, "invalid_stage", "Unknown stage.")
    stmt = (
        select(Application, Candidate, Job)
        .join(Candidate, Candidate.id == Application.candidate_id)
        .join(Job, Job.id == Application.job_id)
        .where(Application.organization_id == ctx.org.id)
    )
    if job_id:
        stmt = stmt.where(Application.job_id == job_id)
    if stage:
        stmt = stmt.where(Application.stage == stage)
    if q:
        term = _like(q)
        stmt = stmt.where(or_(func.lower(Candidate.full_name).like(term, escape="\\"), func.lower(Candidate.email).like(term, escape="\\")))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.execute(stmt.order_by(Application.updated_at.desc(), Application.created_at.desc()).limit(limit).offset(offset)).all()
    return {"items": [application_out(a, c, j) for a, c, j in rows], "total": int(total)}


@router.get("/applications/{application_id}")
def get_application(application_id: str, ctx: OrgContext = Depends(require("applications.read")), db: Session = Depends(get_db)):
    a = _application(db, ctx, application_id)
    c, j = db.get(Candidate, a.candidate_id), db.get(Job, a.job_id)
    history = db.scalars(
        select(ApplicationStageHistory).where(ApplicationStageHistory.application_id == a.id).order_by(ApplicationStageHistory.created_at)
    ).all()
    out = application_out(a, c, j)
    out["candidate"] = candidate_out(c)
    out["job"] = job_out(j)
    out["history"] = [
        {"from_stage": h.from_stage, "to_stage": h.to_stage, "reason": h.reason,
         "changed_by_user_id": h.changed_by_user_id, "created_at": h.created_at.isoformat()}
        for h in history
    ]
    out["handoff"] = handoff_service.serialize(db.scalar(select(HiringHandoff).where(HiringHandoff.application_id == a.id)))
    if can(ctx.role, "evaluation.read"):
        latest = db.scalar(select(Evaluation).where(Evaluation.application_id == a.id).order_by(Evaluation.created_at.desc()))
        out["latest_evaluation"] = evaluation_service.serialize(latest) if latest else None
    out["allowed_transitions"] = _allowed(a.stage) if can(ctx.role, "applications.transition") else []
    return out


def _allowed(stage: str) -> list[str]:
    options = []
    for target in STAGES:
        try:
            validate_transition(stage, target, reason="x")
        except TransitionError:
            continue
        options.append(target)
    return options


@router.patch("/applications/{application_id}")
def update_application(
    application_id: str, body: ApplicationPatch, ctx: OrgContext = Depends(require("applications.write")), db: Session = Depends(get_db)
):
    a = _application(db, ctx, application_id)
    changes = body.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(a, field, value)
    audit.record(db, organization_id=ctx.org.id, actor_user_id=ctx.user_id, action="application.updated",
                 entity_type="application", entity_id=a.id, fields=sorted(changes))
    db.commit()
    return application_out(a, db.get(Candidate, a.candidate_id), db.get(Job, a.job_id))


@router.post("/applications/{application_id}/transitions")
def transition_application(
    application_id: str, body: TransitionIn, ctx: OrgContext = Depends(require("applications.transition")), db: Session = Depends(get_db)
):
    a = _application(db, ctx, application_id)
    try:
        t = validate_transition(a.stage, body.to_stage, reason=body.reason)
    except TransitionError as exc:
        raise _err(422, "invalid_transition", str(exc), from_stage=a.stage, to_stage=body.to_stage)
    # Compare-and-set: only move it if it is STILL in the stage we validated against, so two
    # people acting at once cannot both succeed from the same starting point.
    moved = db.execute(
        update(Application)
        .where(Application.id == a.id, Application.organization_id == ctx.org.id, Application.stage == t.from_stage)
        .values(stage=t.to_stage, updated_at=utcnow())
    )
    if moved.rowcount != 1:
        db.rollback()
        raise _err(409, "stage_changed", "Someone else moved this application. Reload and try again.")
    db.add(ApplicationStageHistory(
        organization_id=ctx.org.id, application_id=a.id, from_stage=t.from_stage, to_stage=t.to_stage,
        changed_by_user_id=ctx.user_id, reason=(body.reason or None),
    ))
    audit.record(db, organization_id=ctx.org.id, actor_user_id=ctx.user_id, action="application.stage_changed",
                 entity_type="application", entity_id=a.id, from_stage=t.from_stage, to_stage=t.to_stage)
    db.commit()
    db.refresh(a)
    return application_out(a, db.get(Candidate, a.candidate_id), db.get(Job, a.job_id))


# --- scorecards ------------------------------------------------------------------------------
@router.post("/applications/{application_id}/scorecards", status_code=201)
def create_scorecard(
    application_id: str, body: ScorecardIn, ctx: OrgContext = Depends(require("scorecards.write")), db: Session = Depends(get_db)
):
    a = _application(db, ctx, application_id)
    if a.stage in ("rejected", "withdrawn"):
        raise _err(409, "application_closed", "Reopen this application before adding feedback.")
    s = Scorecard(
        organization_id=ctx.org.id, application_id=a.id, interviewer_user_id=ctx.user_id,
        interviewer_name=body.interviewer_name or ctx.principal.name or None,
        overall=Decimal(str(body.overall)), technical=Decimal(str(body.technical)) if body.technical is not None else None,
        culture=Decimal(str(body.culture)) if body.culture is not None else None,
        notes=body.notes, interview_date=body.interview_date,
    )
    db.add(s)
    db.flush()
    audit.record(db, organization_id=ctx.org.id, actor_user_id=ctx.user_id, action="scorecard.created",
                 entity_type="application", entity_id=a.id)
    db.commit()
    return {**scorecard_out(s), "mine": True}


@router.get("/applications/{application_id}/scorecards")
def list_scorecards(application_id: str, ctx: OrgContext = Depends(require("applications.read")), db: Session = Depends(get_db)):
    a = _application(db, ctx, application_id)
    stmt = select(Scorecard).where(Scorecard.application_id == a.id, Scorecard.organization_id == ctx.org.id)
    if can(ctx.role, "scorecards.read_all"):
        pass
    elif can(ctx.role, "scorecards.read_own"):
        stmt = stmt.where(Scorecard.interviewer_user_id == ctx.user_id)
    else:
        raise _err(403, "forbidden", "Your role does not allow reading scorecards.")
    rows = db.scalars(stmt.order_by(Scorecard.created_at.desc())).all()
    items = []
    for s in rows:
        out = scorecard_out(s)
        out["mine"] = s.interviewer_user_id == ctx.user_id
        items.append(out)
    return {"items": items}


# --- handoff ---------------------------------------------------------------------------------
@router.post("/applications/{application_id}/handoff")
def request_handoff(
    application_id: str, body: HandoffIn, request: Request,
    ctx: OrgContext = Depends(require("handoff.request")), db: Session = Depends(get_db),
):
    a = _application(db, ctx, application_id)
    state = request.app.state
    try:
        h = handoff_service.request_handoff(
            db, ctx, a, start_date=body.start_date, settings=state.settings,
            client_factory=getattr(state, "hrms_client_factory", None),
        )
    except handoff_service.HandoffUnavailable as exc:
        raise _err(exc.http_status, exc.code, exc.message)
    return handoff_service.serialize(h)


@router.get("/applications/{application_id}/handoff")
def get_handoff(application_id: str, ctx: OrgContext = Depends(require("applications.read")), db: Session = Depends(get_db)):
    a = _application(db, ctx, application_id)
    return {"handoff": handoff_service.serialize(db.scalar(select(HiringHandoff).where(HiringHandoff.application_id == a.id)))}


# --- evaluations -----------------------------------------------------------------------------
@router.post("/applications/{application_id}/evaluations", status_code=201)
def run_evaluation(
    application_id: str, request: Request, ctx: OrgContext = Depends(require("evaluation.run")), db: Session = Depends(get_db)
):
    a = _application(db, ctx, application_id)
    state = request.app.state
    factory = getattr(state, "evaluation_provider_factory", None)
    try:
        e = evaluation_service.run_evaluation(
            db, ctx, a, settings=state.settings, provider=factory() if factory else None
        )
    except evaluation_service.EvaluationUnavailable as exc:
        raise _err(exc.http_status, exc.code, exc.message)
    return evaluation_service.serialize(e)


@router.get("/applications/{application_id}/evaluations")
def list_evaluations(application_id: str, ctx: OrgContext = Depends(require("evaluation.read")), db: Session = Depends(get_db)):
    a = _application(db, ctx, application_id)
    rows = db.scalars(select(Evaluation).where(Evaluation.application_id == a.id).order_by(Evaluation.created_at.desc())).all()
    return {"items": [evaluation_service.serialize(e) for e in rows]}


# --- stats -----------------------------------------------------------------------------------
@router.get("/stats")
def stats(ctx: OrgContext = Depends(require("applications.read")), db: Session = Depends(get_db)):
    by_stage = dict(
        db.execute(
            select(Application.stage, func.count()).where(Application.organization_id == ctx.org.id).group_by(Application.stage)
        ).all()
    )
    open_jobs = db.scalar(select(func.count()).select_from(Job).where(Job.organization_id == ctx.org.id, Job.status == "open")) or 0
    candidates = db.scalar(select(func.count()).select_from(Candidate).where(Candidate.organization_id == ctx.org.id)) or 0
    return {
        "open_jobs": int(open_jobs),
        "candidates": int(candidates),
        "by_stage": {s: int(by_stage.get(s, 0)) for s in STAGES},
        "open_applications": int(sum(by_stage.get(s, 0) for s in OPEN_STAGES)),
    }
