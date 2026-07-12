from __future__ import annotations

import hashlib
import hmac
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken


class TokenCipher:
    """Encrypts OAuth credentials before database persistence."""

    def __init__(self, key: str) -> None:
        try:
            self._fernet = Fernet(key.encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise ValueError("MAIL_TOKEN_ENCRYPTION_KEY must be a valid Fernet key") from exc

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("utf-8")

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("Unable to decrypt mailbox token") from exc


@dataclass(frozen=True)
class WebhookVerifier:
    shared_secret: bytes

    @classmethod
    def from_string(cls, secret: str) -> "WebhookVerifier":
        if len(secret) < 32:
            raise ValueError("Webhook secret must be at least 32 characters")
        return cls(shared_secret=secret.encode("utf-8"))

    def sign(self, body: bytes) -> str:
        digest = hmac.new(self.shared_secret, body, hashlib.sha256).digest()
        return urlsafe_b64encode(digest).decode("ascii")

    def verify(self, body: bytes, signature: str) -> bool:
        try:
            supplied = urlsafe_b64decode(signature.encode("ascii"))
        except Exception:
            return False
        expected = hmac.new(self.shared_secret, body, hashlib.sha256).digest()
        return hmac.compare_digest(expected, supplied)
