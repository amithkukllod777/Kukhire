"""Authentication: who is calling, via the shared Kuklabs Account.

KukHire has no users table, passwords, OTPs or sessions of its own (KUKLABS_IDENTITY.md). A caller
presents the AuthKit access token as ``Authorization: Bearer <token>``; this module asks the
identity server — the documented contract, ``GET /v1/auth/me`` — whether the token is valid and who
it belongs to. It does NOT parse or verify the token locally: that would be inventing token
validation the identity contract does not give separate-repo apps.

Authentication only answers "who". Whether that person may open a given workspace is answered by
``app.tenancy`` from KukHire's own membership table.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Protocol

import httpx
from fastapi import Header, HTTPException, Request, status


@dataclass(frozen=True)
class Principal:
    user_id: str
    email: str | None
    email_verified: bool
    name: str


class IdentityUnavailable(RuntimeError):
    """The identity server could not be reached or answered unexpectedly."""


class IdentityVerifier(Protocol):
    def verify(self, token: str) -> Principal | None:
        """The principal for a valid token; None if the token is invalid or expired.
        Raises IdentityUnavailable when validity cannot be determined."""


class AuthKitRestVerifier:
    def __init__(
        self,
        base_url: str,
        *,
        product_id: str = "kukhire",
        timeout: float = 5.0,
        cache_seconds: int = 30,
        client: httpx.Client | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        self._url = f"{base_url.rstrip('/')}/v1/auth/me"
        self._product = product_id
        self._timeout = timeout
        self._ttl = max(0, cache_seconds)
        self._client = client or httpx.Client(timeout=timeout)
        # sha256(token) -> (principal, expires_at). Positive results only: a revoked token must
        # fail on the next call after the TTL, and a bad token should never be remembered as good.
        self._cache: dict[str, tuple[Principal, float]] = {}

    def verify(self, token: str) -> Principal | None:
        key = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and hit[1] > now:
            return hit[0]
        try:
            response = self._client.get(
                self._url,
                headers={"Authorization": f"Bearer {token}", "X-Kuklabs-Product": self._product},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise IdentityUnavailable("identity server unreachable") from exc
        if response.status_code in (401, 403):
            self._cache.pop(key, None)
            return None
        if response.status_code != 200:
            raise IdentityUnavailable(f"identity server answered {response.status_code}")
        try:
            user = response.json()["user"]
            user_id = str(user["kuklabs_user_id"])
        except (ValueError, KeyError, TypeError) as exc:
            raise IdentityUnavailable("identity server returned an unexpected body") from exc
        if not user_id:
            raise IdentityUnavailable("identity server returned an empty user id")
        principal = Principal(
            user_id=user_id,
            email=(user.get("email") or None),
            email_verified=bool(user.get("email_verified")),
            name=str(user.get("full_name") or ""),
        )
        if self._ttl:
            if len(self._cache) > 2048:
                self._cache.clear()
            self._cache[key] = (principal, now + self._ttl)
        return principal


def current_principal(request: Request, authorization: str = Header(default="")) -> Principal:
    verifier: IdentityVerifier | None = getattr(request.app.state, "verifier", None)
    if verifier is None:
        # Fail closed: an unconfigured identity server must not mean "everyone is welcome".
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "identity_not_configured", "message": "Set KUKHIRE_AUTH_BASE_URL."},
        )
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthenticated", "message": "Sign in required."},
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        principal = verifier.verify(token.strip())
    except IdentityUnavailable:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "identity_unavailable", "message": "Could not verify your session. Try again."},
        )
    if principal is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthenticated", "message": "Session expired. Please sign in again."},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal
