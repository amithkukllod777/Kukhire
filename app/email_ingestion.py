from __future__ import annotations

from enum import Enum
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, EmailStr, Field


class MailProvider(str, Enum):
    GMAIL = "gmail"
    MICROSOFT = "microsoft"
    IMAP = "imap"


class AttachmentInput(BaseModel):
    filename: str
    content_type: str
    storage_key: str
    size_bytes: int = Field(ge=0)


class CandidateEmailInput(BaseModel):
    provider: MailProvider
    mailbox_id: str
    external_message_id: str
    thread_id: str | None = None
    sender_name: str | None = None
    sender_email: EmailStr
    subject: str = ""
    body_text: str = ""
    received_at: str
    job_id: str | None = None
    attachments: list[AttachmentInput] = Field(default_factory=list)


class CandidateIngestionResult(BaseModel):
    ingestion_id: str
    status: str
    candidate_id: str | None = None
    application_id: str | None = None
    resume_attachment: str | None = None
    duplicate_message: bool = False
    human_review_required: bool = True
    warnings: list[str] = Field(default_factory=list)


class CandidateEmailRepository(Protocol):
    def message_exists(self, mailbox_id: str, external_message_id: str) -> bool: ...

    def save_raw_message(self, payload: CandidateEmailInput) -> None: ...


class CandidateEmailIngestionService:
    """Provider-neutral email ingestion boundary.

    Provider adapters fetch messages and attachments. This service owns
    validation, idempotency, resume selection, and downstream job creation.
    It deliberately does not store OAuth tokens or call Gmail/IMAP directly.
    """

    RESUME_CONTENT_TYPES = {
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }

    def __init__(self, repository: CandidateEmailRepository):
        self.repository = repository

    def ingest(self, payload: CandidateEmailInput) -> CandidateIngestionResult:
        ingestion_id = str(uuid4())

        if self.repository.message_exists(payload.mailbox_id, payload.external_message_id):
            return CandidateIngestionResult(
                ingestion_id=ingestion_id,
                status="ignored",
                duplicate_message=True,
                warnings=["Message already ingested."],
            )

        self.repository.save_raw_message(payload)
        resumes = [
            attachment
            for attachment in payload.attachments
            if attachment.content_type in self.RESUME_CONTENT_TYPES
        ]

        if not resumes:
            return CandidateIngestionResult(
                ingestion_id=ingestion_id,
                status="needs_review",
                warnings=["No supported resume attachment found."],
            )

        selected = max(resumes, key=lambda item: item.size_bytes)
        warnings: list[str] = []
        if len(resumes) > 1:
            warnings.append("Multiple resume files found; largest supported file selected.")

        return CandidateIngestionResult(
            ingestion_id=ingestion_id,
            status="queued",
            resume_attachment=selected.storage_key,
            warnings=warnings,
        )
