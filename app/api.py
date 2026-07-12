from uuid import uuid4

from fastapi import FastAPI, HTTPException, status

from app.schemas import (
    CapabilitiesResponse,
    Capability,
    EvaluationRequest,
    EvaluationResponse,
    HealthResponse,
)

API_VERSION = "0.1.0"

app = FastAPI(
    title="KukHire API",
    description=(
        "AI-assisted applicant tracking and explainable candidate evaluation. "
        "All AI recommendations require human review."
    ),
    version=API_VERSION,
)


@app.get("/health", response_model=HealthResponse, tags=["system"])
def health() -> HealthResponse:
    return HealthResponse(version=API_VERSION)


@app.get("/v1/capabilities", response_model=CapabilitiesResponse, tags=["system"])
def capabilities() -> CapabilitiesResponse:
    return CapabilitiesResponse(
        capabilities=[
            Capability(key="resume_parsing", name="Resume parsing", status="existing-engine"),
            Capability(key="github_enrichment", name="GitHub enrichment", status="existing-engine"),
            Capability(key="job_scoring", name="Job-specific scoring", status="planned"),
            Capability(key="ats_pipeline", name="Hiring pipeline", status="planned"),
            Capability(key="interviews", name="Interview workflows", status="planned"),
        ]
    )


@app.post(
    "/v1/evaluations",
    response_model=EvaluationResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["evaluations"],
)
def create_evaluation(payload: EvaluationRequest) -> EvaluationResponse:
    if not payload.job_id.strip() or not payload.candidate_id.strip():
        raise HTTPException(status_code=422, detail="job_id and candidate_id are required")

    return EvaluationResponse(
        evaluation_id=str(uuid4()),
        status="queued",
        message=(
            "Evaluation request accepted. The scoring worker and persistence layer "
            "will be connected in the next implementation phase."
        ),
        details={
            "job_id": payload.job_id,
            "candidate_id": payload.candidate_id,
            "human_review_required": True,
        },
    )
