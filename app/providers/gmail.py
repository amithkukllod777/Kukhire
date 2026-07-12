from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Any, Protocol

from app.email_ingestion import AttachmentInput, CandidateEmailInput, MailProvider


class GmailClient(Protocol):
    """Minimal boundary around the Gmail API client.

    OAuth token storage and refresh stay outside this adapter. The implementation
    may use Google APIs, a queue worker, or a mocked client in tests.
    """

    def get_message(self, *, user_id: str, message_id: str) -> dict[str, Any]: ...

    def store_attachment(
        self,
        *,
        user_id: str,
        message_id: str,
        attachment_id: str,
        filename: str,
        content_type: str,
    ) -> tuple[str, int]: ...


@dataclass(frozen=True)
class GmailMessageReference:
    mailbox_id: str
    user_id: str
    message_id: str
    job_id: str | None = None


class GmailMessageAdapter:
    """Maps Gmail API payloads into KukHire's provider-neutral input model."""

    def __init__(self, client: GmailClient):
        self.client = client

    def fetch(self, reference: GmailMessageReference) -> CandidateEmailInput:
        message = self.client.get_message(
            user_id=reference.user_id,
            message_id=reference.message_id,
        )
        payload = message.get("payload") or {}
        headers = {
            item.get("name", "").lower(): item.get("value", "")
            for item in payload.get("headers", [])
        }
        sender_name, sender_email = parseaddr(headers.get("from", ""))
        if not sender_email:
            raise ValueError("Gmail message does not contain a valid From address")

        attachments: list[AttachmentInput] = []
        body_chunks: list[str] = []
        self._walk_parts(
            parts=[payload],
            reference=reference,
            attachments=attachments,
            body_chunks=body_chunks,
        )

        internal_date = message.get("internalDate")
        received_at = self._to_iso8601(internal_date)

        return CandidateEmailInput(
            provider=MailProvider.GMAIL,
            mailbox_id=reference.mailbox_id,
            external_message_id=str(message.get("id") or reference.message_id),
            thread_id=message.get("threadId"),
            sender_name=sender_name or None,
            sender_email=sender_email,
            subject=headers.get("subject", ""),
            body_text="\n\n".join(chunk for chunk in body_chunks if chunk).strip(),
            received_at=received_at,
            job_id=reference.job_id,
            attachments=attachments,
        )

    def _walk_parts(
        self,
        *,
        parts: list[dict[str, Any]],
        reference: GmailMessageReference,
        attachments: list[AttachmentInput],
        body_chunks: list[str],
    ) -> None:
        for part in parts:
            mime_type = part.get("mimeType", "")
            filename = part.get("filename", "")
            body = part.get("body") or {}
            attachment_id = body.get("attachmentId")

            if filename and attachment_id:
                storage_key, size_bytes = self.client.store_attachment(
                    user_id=reference.user_id,
                    message_id=reference.message_id,
                    attachment_id=attachment_id,
                    filename=filename,
                    content_type=mime_type or "application/octet-stream",
                )
                attachments.append(
                    AttachmentInput(
                        filename=filename,
                        content_type=mime_type or "application/octet-stream",
                        storage_key=storage_key,
                        size_bytes=size_bytes,
                    )
                )

            if mime_type == "text/plain" and body.get("decoded_text"):
                body_chunks.append(str(body["decoded_text"]))

            child_parts = part.get("parts") or []
            if child_parts:
                self._walk_parts(
                    parts=child_parts,
                    reference=reference,
                    attachments=attachments,
                    body_chunks=body_chunks,
                )

    @staticmethod
    def _to_iso8601(internal_date: Any) -> str:
        if internal_date is None:
            return datetime.now(timezone.utc).isoformat()
        try:
            milliseconds = int(internal_date)
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid Gmail internalDate") from exc
        return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc).isoformat()
