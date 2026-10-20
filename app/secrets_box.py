"""Encryption for secrets at rest (the org's HRMS API key). Fernet, key from configuration."""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class SecretBoxUnavailable(RuntimeError):
    pass


class SecretBox:
    def __init__(self, key: str) -> None:
        if not key:
            raise SecretBoxUnavailable("KUKHIRE_SECRET_ENCRYPTION_KEY is not set")
        try:
            self._fernet = Fernet(key.encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise SecretBoxUnavailable("KUKHIRE_SECRET_ENCRYPTION_KEY must be a valid Fernet key") from exc

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("utf-8")

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise SecretBoxUnavailable("stored secret cannot be decrypted with the configured key") from exc
