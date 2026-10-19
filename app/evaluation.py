"""Evaluation SERVICE: persistence, tenancy and audit around the shared evaluation core.

Advisory only. Nothing here changes an application's stage or score; the API answers
"unavailable" explicitly when it cannot run (see app/evaluation_core.py for the rules).
"""
from __future__ import annotations

import logging
import time

from sqlalchemy.orm import Session

from app import audit
from app.config import Settings
from app.db_models import Application, Candidate, Evaluation, Job
from app.evaluation_core import (  # noqa: F401  (re-exported: callers and tests import these from here)
    REDACTION_VERSION,
    EvaluationFailure,
    EvaluationOutput,
    EvaluationProvider,
    EvaluationUnavailable,
    OllamaEvaluationProvider,
    build_prompts,
    default_provider,
    evaluate_material,
    redact_resume,
)
from app.scoring import CriteriaError, validate_criteria
from app.tenancy import OrgContext

# Metadata only. Never log prompts, resume material or model output — they are candidate data.
logger = logging.getLogger("kukhire.evaluation")


def serialize(e: Evaluation) -> dict:
    return {
        "id": e.id,
        "application_id": e.application_id,
        "status": e.status,
        "advisory": True,  # always; there is no mode in which an evaluation decides anything
        "provider": e.provider,
        "model": e.model,
        "overall": float(e.overall) if e.overall is not None else None,
        "criteria": (e.result or {}).get("criteria", []),
        "summary": (e.result or {}).get("summary", ""),
        "risks": (e.result or {}).get("risks", []),
        "error": e.error,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


def run_evaluation(
    db: Session,
    ctx: OrgContext,
    application: Application,
    *,
    settings: Settings,
    provider: EvaluationProvider | None = None,
) -> Evaluation:
    if not settings.evaluation_enabled:
        raise EvaluationUnavailable(
            "evaluation_not_enabled", "AI evaluation is not enabled for this deployment.", 503
        )
    job = db.get(Job, application.job_id)
    candidate = db.get(Candidate, application.candidate_id)
    if job is None or candidate is None or application.organization_id != ctx.org.id:
        raise EvaluationUnavailable("not_found", "Application not found.", 404)

    try:
        criteria = validate_criteria(job.scoring_criteria)
    except CriteriaError:
        raise EvaluationUnavailable(
            "no_scoring_criteria", "This job has no valid scoring criteria. Add criteria to the job first.", 422
        )

    provider = provider or default_provider(settings.evaluation_provider, settings.evaluation_model)
    job_view = {
        "title": job.title, "department": job.department, "employment_type": job.employment_type,
        "description": job.description, "requirements": job.requirements,
    }
    started = time.monotonic()
    try:
        output, overall = evaluate_material(job_view, criteria, candidate.parsed_resume, provider)
    except EvaluationFailure as failure:
        db.add(
            Evaluation(
                organization_id=ctx.org.id, application_id=application.id, job_id=job.id, status="failed",
                provider=provider.name, model=provider.model, criteria_snapshot=criteria, result=None,
                error=failure.code, advisory=True, created_by_user_id=ctx.user_id,
            )
        )
        audit.record(db, organization_id=ctx.org.id, actor_user_id=ctx.user_id, action="evaluation.failed",
                     entity_type="application", entity_id=application.id, reason=failure.code)
        db.commit()
        logger.warning("evaluation failed org=%s application=%s reason=%s", ctx.org.id, application.id, failure.code)
        raise EvaluationUnavailable(failure.code, failure.message, 502)

    evaluation = Evaluation(
        organization_id=ctx.org.id, application_id=application.id, job_id=job.id, status="completed",
        provider=provider.name, model=provider.model, criteria_snapshot=criteria,
        result=output.model_dump(), overall=overall, advisory=True, created_by_user_id=ctx.user_id,
    )
    db.add(evaluation)
    audit.record(db, organization_id=ctx.org.id, actor_user_id=ctx.user_id, action="evaluation.completed",
                 entity_type="application", entity_id=application.id, redaction_version=REDACTION_VERSION)
    db.commit()
    logger.info(
        "evaluation completed org=%s application=%s ms=%d", ctx.org.id, application.id,
        int((time.monotonic() - started) * 1000),
    )
    return evaluation
