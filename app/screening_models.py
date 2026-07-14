from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.db_models import new_id


class IngestedEmail(Base):
    __tablename__ = "ingested_emails"
    __table_args__ = (
        UniqueConstraint("mailbox_id", "external_message_id", name="uq_ingested_email_message"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    mailbox_id: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    external_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    thread_id: Mapped[str | None] = mapped_column(String(255))
    sender_name: Mapped[str | None] = mapped_column(String(180))
    sender_email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    subject: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    body_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    received_at: Mapped[str] = mapped_column(String(64), nullable=False)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"), index=True)
    attachments: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="received", nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class MailboxConnection(Base):
    __tablename__ = "mailbox_connections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    email_address: Mapped[str] = mapped_column(String(320), nullable=False)
    encrypted_access_token: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_refresh_token: Mapped[str | None] = mapped_column(Text)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    watch_cursor: Mapped[str | None] = mapped_column(String(500))
    active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
