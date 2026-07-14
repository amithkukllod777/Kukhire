from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.email_ingestion import CandidateEmailIngestionService, CandidateEmailInput, CandidateIngestionResult


class ResumeParser(Protocol):
    def enqueue(self, *, storage_key: str, candidate_email: str) -> str: ...


class CandidateResolver(Protocol):
    def resolve_or_create(self, *, email: str, name: str | None) -> str: ...


class ApplicationResolver(Protocol):
    def create_or_get(self, *, candidate_id: str, job_id: str | None, source: str) -> str | None: ...


@dataclass(frozen=True)
class PipelineResult:
    ingestion: CandidateIngestionResult
    parsing_job_id: str | None = None


class EmailCandidatePipeline:
    """Coordinates ingestion, candidate resolution, application creation and parsing.

    The pipeline only advances messages that contain a supported resume. Missing
    resumes and uncertain job matches remain visible for recruiter review.
    """

    def __init__(
        self,
        *,
        ingestion_service: CandidateEmailIngestionService,
        candidate_resolver: CandidateResolver,
        application_resolver: ApplicationResolver,
        resume_parser: ResumeParser,
    ) -> None:
        self.ingestion_service = ingestion_service
        self.candidate_resolver = candidate_resolver
        self.application_resolver = application_resolver
        self.resume_parser = resume_parser

    def process(self, payload: CandidateEmailInput) -> PipelineResult:
        result = self.ingestion_service.ingest(payload)
        if result.status != "queued" or not result.resume_attachment:
            return PipelineResult(ingestion=result)

        candidate_id = self.candidate_resolver.resolve_or_create(
            email=str(payload.sender_email),
            name=payload.sender_name,
        )
        application_id = self.application_resolver.create_or_get(
            candidate_id=candidate_id,
            job_id=payload.job_id,
            source="email",
        )
        parsing_job_id = self.resume_parser.enqueue(
            storage_key=result.resume_attachment,
            candidate_email=str(payload.sender_email),
        )

        result.candidate_id = candidate_id
        result.application_id = application_id
        result.human_review_required = payload.job_id is None
        if payload.job_id is None:
            result.warnings.append("No job matched; recruiter must assign this candidate.")

        return PipelineResult(ingestion=result, parsing_job_id=parsing_job_id)
