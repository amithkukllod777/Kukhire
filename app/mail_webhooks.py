from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.mail_security import WebhookVerifier


class GmailNotification(BaseModel):
    mailbox_id: str = Field(min_length=1)
    user_id: str = Field(default="me", min_length=1)
    message_id: str = Field(min_length=1)
    job_id: str | None = None


class WebhookAccepted(BaseModel):
    status: str = "accepted"
    delivery_id: str


class WebhookDependencies:
    def __init__(
        self,
        *,
        verifier: WebhookVerifier,
        enqueue: Callable[[GmailNotification], str],
    ) -> None:
        self.verifier = verifier
        self.enqueue = enqueue


def build_mail_webhook_router(dependencies: WebhookDependencies) -> APIRouter:
    router = APIRouter(prefix="/v1/mail", tags=["mail"])

    @router.post(
        "/gmail/notifications",
        response_model=WebhookAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def gmail_notification(
        request: Request,
        x_kukhire_signature: str = Header(default=""),
    ) -> WebhookAccepted:
        raw_body = await request.body()
        if not dependencies.verifier.verify(raw_body, x_kukhire_signature):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

        try:
            payload = GmailNotification.model_validate_json(raw_body)
            delivery_id = dependencies.enqueue(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        return WebhookAccepted(delivery_id=delivery_id)

    return router
