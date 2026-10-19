"""Client for the HRMS-owned hire handoff (KukBook: ``POST /api/v1/hr/hires``).

KukHire never writes employee tables. It asks HRMS, over this one authenticated call, to create the
employee, and keeps only the reference HRMS returns. The call is idempotent on
``externalApplicationRef``, so retrying after a timeout is always safe.

Safety rules baked in here:
* the target host must be on the operator's allowlist (``KUKHIRE_HRMS_ALLOWED_HOSTS``) — an org
  admin types the URL, so without this the server could be pointed at an internal service;
* redirects are never followed — a redirect would carry the company API key to another host;
* the API key is never logged and never appears in an error message.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx

HIRES_PATH = "/api/v1/hr/hires"


class HandoffRejected(RuntimeError):
    """HRMS refused the request and a retry will not help (bad key/scope, invalid data)."""


class HandoffTransient(RuntimeError):
    """The outcome is unknown or HRMS is temporarily unavailable. Retrying is safe."""


def normalize_hrms_base_url(url: str, *, allowed_hosts: tuple[str, ...], allow_insecure_http: bool) -> str:
    """Validate an admin-supplied HRMS URL and return its origin (scheme://host[:port])."""
    parts = urlsplit((url or "").strip())
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ValueError("The HRMS URL must start with https://")
    if parts.scheme == "http" and not allow_insecure_http:
        raise ValueError("The HRMS URL must use https")
    if parts.username or parts.password:
        raise ValueError("The HRMS URL must not contain credentials")
    if parts.query or parts.fragment or parts.path not in ("", "/"):
        raise ValueError("The HRMS URL must be just the origin, e.g. https://host")
    host_key = parts.netloc.lower()
    if host_key not in allowed_hosts:
        raise ValueError("This HRMS host is not allowed by this KukHire deployment")
    return f"{parts.scheme}://{host_key}"


class HrmsClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._client = httpx.Client(timeout=timeout, follow_redirects=False, transport=transport)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}", "Accept": "application/json"}

    def _send(self, method: str, path: str, body: dict | None = None) -> httpx.Response:
        try:
            return self._client.request(method, f"{self._base}{path}", json=body, headers=self._headers())
        except httpx.HTTPError as exc:
            # Do not include str(exc): some transports echo the request line.
            raise HandoffTransient("HRMS could not be reached") from exc

    @staticmethod
    def _error_text(response: httpx.Response) -> str:
        try:
            message = response.json().get("error")
        except ValueError:
            message = None
        return (str(message) if message else f"HRMS answered {response.status_code}")[:200]

    def create_hire(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._send("POST", HIRES_PATH, payload)
        status = response.status_code
        if status in (200, 201):
            try:
                data = response.json()["data"]
                if not isinstance(data.get("employeeId"), int) or data["employeeId"] <= 0:
                    raise ValueError
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise HandoffTransient("HRMS returned an unexpected response") from exc
            return data
        if status in (401, 403):
            raise HandoffRejected("HRMS rejected the API key or its permissions")
        if 300 <= status < 400:
            raise HandoffRejected("HRMS answered with a redirect, which is not followed")
        if status in (400, 404, 422):
            raise HandoffRejected(self._error_text(response))
        # 409, 429, 5xx and anything unexpected: the outcome is not a verdict on the data.
        raise HandoffTransient(self._error_text(response))

    def get_hire(self, external_ref: str) -> dict[str, Any] | None:
        response = self._send("GET", f"{HIRES_PATH}/{external_ref}")
        if response.status_code == 404:
            return None
        if response.status_code == 200:
            try:
                return response.json()["data"]
            except (ValueError, KeyError) as exc:
                raise HandoffTransient("HRMS returned an unexpected response") from exc
        if response.status_code in (401, 403):
            raise HandoffRejected("HRMS rejected the API key or its permissions")
        raise HandoffTransient(self._error_text(response))
