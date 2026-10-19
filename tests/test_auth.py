"""Authentication: the documented AuthKit contract, and fail-closed behaviour."""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.auth import AuthKitRestVerifier, IdentityUnavailable
from app.config import Settings


def _verifier(handler, **kw) -> AuthKitRestVerifier:
    return AuthKitRestVerifier(
        "https://auth.example.com/", client=httpx.Client(transport=httpx.MockTransport(handler)), **kw
    )


ME_OK = {"user": {"kuklabs_user_id": "42", "email": "a@example.com", "email_verified": True, "full_name": "Ada"}}


def test_valid_token_yields_a_principal_and_sends_the_contract_headers():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["auth"], seen["product"] = str(request.url), request.headers["authorization"], request.headers["x-kuklabs-product"]
        return httpx.Response(200, json=ME_OK)

    p = _verifier(handler).verify("tok")
    assert (p.user_id, p.email, p.email_verified, p.name) == ("42", "a@example.com", True, "Ada")
    assert seen == {"url": "https://auth.example.com/v1/auth/me", "auth": "Bearer tok", "product": "kukhire"}


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_token_is_none_not_an_error(status):
    assert _verifier(lambda r: httpx.Response(status, json={"error": True})).verify("bad") is None


@pytest.mark.parametrize("status", [500, 502, 429, 404])
def test_unexpected_status_is_unavailable_not_unauthenticated(status):
    with pytest.raises(IdentityUnavailable):
        _verifier(lambda r: httpx.Response(status)).verify("t")


def test_network_failure_is_unavailable():
    def boom(request):
        raise httpx.ConnectError("down")

    with pytest.raises(IdentityUnavailable):
        _verifier(boom).verify("t")


@pytest.mark.parametrize("body", [{}, {"user": {}}, {"user": {"kuklabs_user_id": ""}}, "not json"])
def test_malformed_identity_body_is_unavailable_never_a_guessed_user(body):
    def handler(request):
        return httpx.Response(200, content=b"not json") if body == "not json" else httpx.Response(200, json=body)

    with pytest.raises(IdentityUnavailable):
        _verifier(handler).verify("t")


def test_positive_results_are_cached_briefly_and_failures_are_not():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json=ME_OK) if request.headers["authorization"] == "Bearer good" else httpx.Response(401)

    v = _verifier(handler, cache_seconds=60)
    v.verify("good"); v.verify("good")
    assert calls["n"] == 1
    v.verify("bad"); v.verify("bad")
    assert calls["n"] == 3  # a bad token is re-checked every time


def test_cache_can_be_disabled():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json=ME_OK)

    v = _verifier(handler, cache_seconds=0)
    v.verify("t"); v.verify("t")
    assert calls["n"] == 2


class TestRoutes:
    def test_missing_or_malformed_authorization_is_401(self, client):
        for headers in ({}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer "}):
            r = client.get("/v1/me", headers=headers)
            assert r.status_code == 401, headers
            assert r.json()["detail"]["error"] == "unauthenticated"

    def test_invalid_token_is_401(self, client):
        assert client.get("/v1/me", headers={"Authorization": "Bearer nope"}).status_code == 401

    def test_valid_token_works(self, client, identity):
        token = identity.add("alice")
        r = client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200 and r.json()["user"]["id"] == "user-alice"

    def test_fails_closed_when_identity_is_not_configured(self, engine, settings):
        with TestClient(create_app(settings, verifier=None, engine=engine)) as c:
            r = c.get("/v1/me", headers={"Authorization": "Bearer anything"})
            assert r.status_code == 503
            assert r.json()["detail"]["error"] == "identity_not_configured"
            # Nothing authenticated is reachable, including workspace data.
            assert c.get("/v1/organizations/x/jobs", headers={"Authorization": "Bearer anything"}).status_code == 503

    def test_identity_outage_is_503_not_a_login(self, engine, settings):
        class Down:
            def verify(self, token):
                raise IdentityUnavailable("down")

        with TestClient(create_app(settings, verifier=Down(), engine=engine)) as c:
            assert c.get("/v1/me", headers={"Authorization": "Bearer t"}).status_code == 503

    def test_public_endpoints_need_no_login_and_leak_no_secrets(self, client):
        assert client.get("/health").status_code == 200
        assert client.get("/ready").json() == {"status": "ready"}
        cfg = client.get("/config.json").json()
        assert set(cfg) == {"auth_base_url", "product", "version"}

    def test_there_is_no_login_or_signup_route_in_kukhire(self, client):
        paths = set(client.get("/openapi.json").json()["paths"])
        assert not [p for p in paths if any(w in p.lower() for w in ("login", "signup", "register", "password", "otp"))]

    def test_settings_default_has_no_identity_host(self):
        assert Settings().auth_base_url == ""


class TestConfigAndImport:
    def test_database_url_precedence_explicit_then_new_env_then_legacy_env_then_default(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("KUKHIRE_DATABASE_URL", raising=False)
        assert Settings().database_url == "sqlite:///./kukhire.db"
        monkeypatch.setenv("DATABASE_URL", "sqlite:///legacy-name.db")
        assert Settings().database_url == "sqlite:///legacy-name.db"  # the name the old Settings read
        monkeypatch.setenv("KUKHIRE_DATABASE_URL", "sqlite:///new-name.db")
        assert Settings().database_url == "sqlite:///new-name.db"  # the new name wins over the legacy one
        assert Settings(database_url="sqlite:///explicit.db").database_url == "sqlite:///explicit.db"  # explicit beats both

    def test_importing_the_api_module_needs_no_database_driver(self):
        import subprocess, sys

        code = "import app.api; print('imported')"
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(__import__('pathlib').Path(__file__).resolve().parent.parent),
                             env={**__import__('os').environ, "DATABASE_URL": "mysql://nobody@127.0.0.1/none"})
        assert out.returncode == 0 and "imported" in out.stdout, out.stderr
