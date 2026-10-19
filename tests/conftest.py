"""Shared fixtures.

* The schema ALWAYS comes from the real migrations — never ``create_all`` — so every test is also a
  test of the migrations.
* ``KUKHIRE_TEST_DATABASE_URL`` selects the database (default: a fresh SQLite file per test). Point
  it at an EMPTY, THROWAWAY PostgreSQL database to run the same suite on production's dialect; the
  fixture wipes its ``public`` schema before every test, so never aim it at real data.
* ``FakeIdentity`` stands in for the AuthKit identity server (``GET /v1/auth/me``). It exists only
  here; the application never contains a token-validation shortcut.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import migrations
from app.api import create_app
from app.auth import Principal
from app.config import Settings
from app.database import build_engine

TEST_DB = os.environ.get("KUKHIRE_TEST_DATABASE_URL", "")


class FakeIdentity:
    """verify(token) -> Principal, for tokens registered with ``add``."""

    def __init__(self) -> None:
        self._by_token: dict[str, Principal] = {}
        self.calls = 0

    def add(self, name: str, *, email: str | None = None, verified: bool = True, user_id: str | None = None) -> str:
        token = f"token-{name}"
        self._by_token[token] = Principal(
            user_id=user_id or f"user-{name}",
            email=email if email is not None else f"{name}@example.com",
            email_verified=verified,
            name=name.title(),
        )
        return token

    def verify(self, token: str) -> Principal | None:
        self.calls += 1
        return self._by_token.get(token)


def _wipe_postgres(url: str) -> None:
    engine = build_engine(url)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()


@pytest.fixture()
def database_url(tmp_path: Path) -> str:
    if TEST_DB:
        if not TEST_DB.startswith("postgresql"):
            raise RuntimeError("KUKHIRE_TEST_DATABASE_URL must be a postgresql:// URL (or unset for SQLite)")
        _wipe_postgres(TEST_DB)
        return TEST_DB
    return f"sqlite:///{tmp_path / 'kukhire-test.db'}"


@pytest.fixture()
def engine(database_url):
    eng = build_engine(database_url)
    migrations.migrate(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def identity() -> FakeIdentity:
    return FakeIdentity()


@pytest.fixture()
def settings(database_url) -> Settings:
    return Settings(
        database_url=database_url,
        auth_base_url="",  # the verifier is injected; the real one is covered in test_auth
        secret_encryption_key=Fernet.generate_key().decode(),
        hrms_allowed_hosts_csv="hrms.example.com",
        evaluation_enabled=False,
        serve_ui=False,
    )


@pytest.fixture()
def make_app(engine, identity, settings):
    def factory(**overrides):
        cfg = overrides.pop("settings", settings)
        return create_app(cfg, verifier=overrides.pop("verifier", identity), engine=engine, **overrides)

    return factory


@pytest.fixture()
def client(make_app):
    with TestClient(make_app()) as c:
        yield c


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class Workspace:
    """A small, ready-made workspace so tests read as intent rather than setup."""

    def __init__(self, client: TestClient, identity: FakeIdentity) -> None:
        self.client, self.identity = client, identity
        self.tokens: dict[str, str] = {}

    def user(self, name: str, **kw) -> dict[str, str]:
        self.tokens[name] = self.identity.add(name, **kw)
        return auth(self.tokens[name])

    def create_org(self, owner: str, name: str = "Acme Hiring") -> str:
        r = self.client.post("/v1/organizations", json={"name": name}, headers=auth(self.tokens[owner]))
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def add_member(self, org_id: str, admin: str, member: str, role: str) -> None:
        """Invite `member` (already registered via user()) and have them claim the invitation."""
        email = self.identity._by_token[self.tokens[member]].email
        r = self.client.post(f"/v1/organizations/{org_id}/members", json={"email": email, "role": role},
                             headers=auth(self.tokens[admin]))
        assert r.status_code == 201, r.text
        me = self.client.get("/v1/me", headers=auth(self.tokens[member]))  # claims the invitation
        assert any(o["id"] == org_id for o in me.json()["organizations"]), me.text

    def h(self, name: str) -> dict[str, str]:
        return auth(self.tokens[name])


@pytest.fixture()
def ws(client, identity) -> Workspace:
    return Workspace(client, identity)


CRITERIA = [
    {"key": "python", "label": "Python depth", "weight": 60, "description": "Backend Python experience"},
    {"key": "communication", "label": "Communication", "weight": 40, "description": ""},
]


@pytest.fixture()
def job_factory(client):
    def make(org_id: str, headers: dict, **over) -> dict:
        body = {"title": "Backend Engineer", "description": "Build services.", "status": "open",
                "department": "Platform", "scoring_criteria": CRITERIA, **over}
        r = client.post(f"/v1/organizations/{org_id}/jobs", json=body, headers=headers)
        assert r.status_code == 201, r.text
        return r.json()

    return make


@pytest.fixture()
def application_factory(client):
    def make(org_id: str, headers: dict, job_id: str, *, email: str = "cand@example.com",
             name: str = "Casey Candidate", **over) -> dict:
        body = {"job_id": job_id, "candidate": {"full_name": name, "email": email}, **over}
        r = client.post(f"/v1/organizations/{org_id}/applications", json=body, headers=headers)
        assert r.status_code == 201, r.text
        return r.json()

    return make
