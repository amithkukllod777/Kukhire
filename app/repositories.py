from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db_models import Application, Candidate, Job
from app.email_ingestion import CandidateEmailInput


class SqlAlchemyEmailRepository:
    """Persists ingested messages with mailbox/message idempotency."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def message_exists(self, mailbox_id: str, external_message_id: str) -> bool:
        from app.screening_models import IngestedEmail

        stmt = select(IngestedEmail.id).where(
            IngestedEmail.mailbox_id == mailbox_id,
            IngestedEmail.external_message_id == external_message_id,
        )
        return self.session.scalar(stmt) is not None

    def save_raw_message(self, payload: CandidateEmailInput) -> None:
        from app.screening_models import IngestedEmail

        record = IngestedEmail(
            mailbox_id=payload.mailbox_id,
            provider=payload.provider.value,
            external_message_id=payload.external_message_id,
            thread_id=payload.thread_id,
            sender_name=payload.sender_name,
            sender_email=str(payload.sender_email),
            subject=payload.subject,
            body_text=payload.body_text,
            received_at=payload.received_at,
            job_id=payload.job_id,
            attachments=[item.model_dump() for item in payload.attachments],
            status="received",
        )
        self.session.add(record)
        try:
            self.session.flush()
        except IntegrityError:
            self.session.rollback()
            raise ValueError("Email message already ingested")


class SqlAlchemyCandidateResolver:
    def __init__(self, session: Session) -> None:
        self.session = session

    def resolve_or_create(self, *, email: str, name: str | None) -> str:
        normalized = email.strip().lower()
        candidate = self.session.scalar(select(Candidate).where(Candidate.email == normalized))
        if candidate is not None:
            if name and (not candidate.full_name or candidate.full_name == normalized):
                candidate.full_name = name
            return candidate.id

        candidate = Candidate(full_name=name or normalized, email=normalized)
        self.session.add(candidate)
        self.session.flush()
        return candidate.id


class SqlAlchemyApplicationResolver:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_or_get(self, *, candidate_id: str, job_id: str | None, source: str) -> str | None:
        if job_id is None:
            return None
        if self.session.get(Job, job_id) is None:
            return None

        stmt = select(Application).where(
            Application.candidate_id == candidate_id,
            Application.job_id == job_id,
        )
        application = self.session.scalar(stmt)
        if application is not None:
            return application.id

        application = Application(candidate_id=candidate_id, job_id=job_id, stage="applied")
        application.evaluation = {"source": source}
        self.session.add(application)
        self.session.flush()
        return application.id
