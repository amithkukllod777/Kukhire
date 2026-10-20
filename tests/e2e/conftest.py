"""End-to-end fixtures: the REAL KukHire API serving the REAL UI, driven by headless Chromium.

KukHire's genuine AuthKitRestVerifier talks over HTTP to FakeAuthServer, so sign-in, token
verification and refresh run the production code path; only the identity server and HRMS are doubles.
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet

from app.api import create_app
from app.config import Settings
from tests.e2e.fakes import FakeAuthServer, FakeHrmsServer, LiveServer

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
HRMS_KEY = "kb_live_E2E_KEY_abcdef123456"


@pytest.fixture(scope="session")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(args=["--no-sandbox"])
        except Exception as exc:  # no browser on this machine
            pytest.skip(f"Chromium is not available: {exc}")
        yield b
        b.close()


@pytest.fixture()
def live(database_url, engine):
    auth, hrms = FakeAuthServer(), FakeHrmsServer(HRMS_KEY)
    auth.add_user("alice@example.com", password="pw-alice", name="Alice Admin")
    auth.add_user("otto@example.com", password="pw-otto", name="Otto Otp", otp="123456")
    auth.add_user("vera@example.com", password="pw-vera", name="Vera Viewer")
    with LiveServer(auth.app) as auth_srv, LiveServer(hrms.app) as hrms_srv:
        settings = Settings(
            database_url=database_url, auth_base_url=auth_srv.url, secret_encryption_key=Fernet.generate_key().decode(),
            hrms_allowed_hosts_csv=f"127.0.0.1:{hrms_srv.port}", hrms_allow_insecure_http=True, serve_ui=True,
            auth_cache_seconds=0,  # so a revoked/expired token is seen at once, and the refresh path is really exercised
        )
        with LiveServer(create_app(settings, engine=engine)) as kuk:
            yield SimpleNamespace(auth=auth, hrms=hrms, kuk=kuk.url, hrms_url=hrms_srv.url, auth_url=auth_srv.url, engine=engine)


@pytest.fixture()
def page(browser, live):
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    pg = context.new_page()
    pg.set_default_timeout(8000)
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    yield pg
    context.close()


def sign_in(page, live, identifier="alice@example.com", password="pw-alice"):
    page.goto(live.kuk + "/")
    page.fill("#identifier", identifier)
    page.fill("#password", password)
    page.click("button[type=submit]")


def token(page) -> str:
    return page.evaluate("JSON.parse(sessionStorage.getItem('kukhire.session')).access_token")


def api(live, page_or_token, method, path, **kw):
    t = page_or_token if isinstance(page_or_token, str) else token(page_or_token)
    return httpx.request(method, live.kuk + path, headers={"Authorization": f"Bearer {t}"}, **kw)


def create_workspace(page, live, name="Acme Hiring"):
    sign_in(page, live)
    page.fill("#org-name", name)
    page.click("#create-org")
    page.wait_for_selector("#role-badge")
