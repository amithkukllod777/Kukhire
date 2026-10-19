from __future__ import annotations

import json
from datetime import date
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

EmploymentType = Literal["full_time", "part_time", "contract", "internship"]
JobStatus = Literal["draft", "open", "on_hold", "closed"]
RoleName = Literal["admin", "recruiter", "interviewer", "viewer"]

MAX_PARSED_RESUME_BYTES = 200_000


class Strict(BaseModel):
    # Unknown fields are an error: a typo'd or malicious extra field must not be silently ignored.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _https_url(value: str | None) -> str | None:
    if value in (None, ""):
        return None
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("must be an https URL")
    if parts.username or parts.password:
        raise ValueError("must not contain credentials")
    if len(value) > 500:
        raise ValueError("must be at most 500 characters")
    return value


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str = "kukhire-api"
    version: str


class Capability(BaseModel):
    key: str
    name: str
    status: str = Field(description="implemented | unavailable | planned")
    detail: str | None = None
    human_review_required: bool = True


class CapabilitiesResponse(BaseModel):
    capabilities: list[Capability]


# --- workspace -------------------------------------------------------------------------------
class OrganizationCreate(Strict):
    name: str = Field(min_length=1, max_length=160)


class MemberInvite(Strict):
    email: EmailStr
    role: RoleName


class MemberUpdate(Strict):
    role: RoleName


class IntegrationIn(Strict):
    hrms_base_url: str = Field(min_length=8, max_length=300)
    api_key: str = Field(min_length=12, max_length=200)


# --- jobs ------------------------------------------------------------------------------------
class JobIn(Strict):
    title: str = Field(min_length=1, max_length=180)
    description: str = Field(min_length=1, max_length=20_000)
    department: str | None = Field(default=None, max_length=100)
    location: str | None = Field(default=None, max_length=200)
    employment_type: EmploymentType = "full_time"
    openings: int = Field(default=1, ge=1, le=1000)
    requirements: str | None = Field(default=None, max_length=20_000)
    closing_date: date | None = None
    status: JobStatus = "draft"
    scoring_criteria: list[dict] | None = None


class JobPatch(Strict):
    title: str | None = Field(default=None, min_length=1, max_length=180)
    description: str | None = Field(default=None, min_length=1, max_length=20_000)
    department: str | None = Field(default=None, max_length=100)
    location: str | None = Field(default=None, max_length=200)
    employment_type: EmploymentType | None = None
    openings: int | None = Field(default=None, ge=1, le=1000)
    requirements: str | None = Field(default=None, max_length=20_000)
    closing_date: date | None = None
    status: JobStatus | None = None
    scoring_criteria: list[dict] | None = None


# --- candidates & applications ---------------------------------------------------------------
class CandidateIn(Strict):
    full_name: str = Field(min_length=1, max_length=180)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=40)
    source: str | None = Field(default=None, max_length=50)
    resume_url: str | None = None
    parsed_resume: dict[str, Any] | None = None

    @field_validator("resume_url")
    @classmethod
    def _resume_url(cls, v: str | None) -> str | None:
        return _https_url(v)

    @field_validator("parsed_resume")
    @classmethod
    def _parsed_size(cls, v: dict | None) -> dict | None:
        if v is not None and len(json.dumps(v)) > MAX_PARSED_RESUME_BYTES:
            raise ValueError("parsed_resume is too large")
        return v


class CandidatePatch(Strict):
    full_name: str | None = Field(default=None, min_length=1, max_length=180)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=40)
    source: str | None = Field(default=None, max_length=50)
    resume_url: str | None = None
    parsed_resume: dict[str, Any] | None = None

    @field_validator("resume_url")
    @classmethod
    def _resume_url(cls, v: str | None) -> str | None:
        return _https_url(v)

    @field_validator("parsed_resume")
    @classmethod
    def _parsed_size(cls, v: dict | None) -> dict | None:
        if v is not None and len(json.dumps(v)) > MAX_PARSED_RESUME_BYTES:
            raise ValueError("parsed_resume is too large")
        return v


class ApplicationCreate(Strict):
    job_id: str
    candidate_id: str | None = None
    candidate: CandidateIn | None = None
    source: str | None = Field(default=None, max_length=50)
    referrer_employee_ref: str | None = Field(default=None, max_length=64)
    referrer_name: str | None = Field(default=None, max_length=160)
    expected_salary: float | None = Field(default=None, ge=0, le=1e12)
    notice_period: str | None = Field(default=None, max_length=50)
    notes: str | None = Field(default=None, max_length=5000)

    @model_validator(mode="after")
    def _one_candidate_source(self) -> "ApplicationCreate":
        if bool(self.candidate_id) == bool(self.candidate):
            raise ValueError("provide exactly one of candidate_id or candidate")
        if self.source == "referral" and not (self.referrer_employee_ref or self.referrer_name):
            raise ValueError("a referral needs referrer_employee_ref or referrer_name")
        return self


class ApplicationPatch(Strict):
    rating: int | None = Field(default=None, ge=1, le=5)
    notes: str | None = Field(default=None, max_length=5000)
    assigned_to: str | None = Field(default=None, max_length=200)
    interview_date: date | None = None
    expected_salary: float | None = Field(default=None, ge=0, le=1e12)
    notice_period: str | None = Field(default=None, max_length=50)


class TransitionIn(Strict):
    to_stage: str
    reason: str | None = Field(default=None, max_length=500)


class ScorecardIn(Strict):
    overall: float = Field(ge=1, le=5)
    technical: float | None = Field(default=None, ge=1, le=5)
    culture: float | None = Field(default=None, ge=1, le=5)
    notes: str | None = Field(default=None, max_length=2000)
    interview_date: date | None = None
    interviewer_name: str | None = Field(default=None, max_length=160)

    @field_validator("overall", "technical", "culture")
    @classmethod
    def _half_points(cls, v: float | None) -> float | None:
        if v is not None and round(v * 2) != v * 2:
            raise ValueError("scores use 0.5 steps")
        return v


class HandoffIn(Strict):
    start_date: date | None = None
