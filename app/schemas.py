from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class HiringStage(str, Enum):
    APPLIED = "applied"
    AI_SCREENED = "ai_screened"
    RECRUITER_REVIEW = "recruiter_review"
    SHORTLISTED = "shortlisted"
    INTERVIEW = "interview"
    ASSESSMENT = "assessment"
    OFFER = "offer"
    HIRED = "hired"
    REJECTED = "rejected"


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str = "kukhire-api"
    version: str


class Capability(BaseModel):
    key: str
    name: str
    status: str = Field(description="Current implementation status")
    human_review_required: bool = True


class CapabilitiesResponse(BaseModel):
    capabilities: list[Capability]


class EvaluationRequest(BaseModel):
    job_id: str
    candidate_id: str
    scoring_weights: dict[str, float] = Field(default_factory=dict)


class EvaluationResponse(BaseModel):
    evaluation_id: str
    status: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
