from app.email_ingestion import (
    AttachmentInput,
    CandidateEmailIngestionService,
    CandidateEmailInput,
    MailProvider,
)


class InMemoryRepository:
    def __init__(self) -> None:
        self.messages: set[tuple[str, str]] = set()

    def message_exists(self, mailbox_id: str, external_message_id: str) -> bool:
        return (mailbox_id, external_message_id) in self.messages

    def save_raw_message(self, payload: CandidateEmailInput) -> None:
        self.messages.add((payload.mailbox_id, payload.external_message_id))


def make_payload(*, attachments: list[AttachmentInput]) -> CandidateEmailInput:
    return CandidateEmailInput(
        provider=MailProvider.GMAIL,
        mailbox_id="mailbox-1",
        external_message_id="message-1",
        sender_email="candidate@example.com",
        subject="Application",
        received_at="2026-07-13T00:00:00+00:00",
        job_id="job-1",
        attachments=attachments,
    )


def test_ingestion_queues_supported_resume() -> None:
    service = CandidateEmailIngestionService(InMemoryRepository())
    result = service.ingest(
        make_payload(
            attachments=[
                AttachmentInput(
                    filename="resume.pdf",
                    content_type="application/pdf",
                    storage_key="resumes/resume.pdf",
                    size_bytes=1200,
                )
            ]
        )
    )

    assert result.status == "queued"
    assert result.resume_attachment == "resumes/resume.pdf"


def test_duplicate_message_is_ignored() -> None:
    repository = InMemoryRepository()
    service = CandidateEmailIngestionService(repository)
    payload = make_payload(attachments=[])

    service.ingest(payload)
    result = service.ingest(payload)

    assert result.status == "ignored"
    assert result.duplicate_message is True


def test_missing_resume_requires_review() -> None:
    service = CandidateEmailIngestionService(InMemoryRepository())
    result = service.ingest(make_payload(attachments=[]))

    assert result.status == "needs_review"
    assert result.human_review_required is True
