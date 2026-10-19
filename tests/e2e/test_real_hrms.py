"""KukHire -> the REAL KukBook ``POST /api/v1/hr/hires`` over HTTP (no HRMS double).

Opt-in: skipped unless both are set, because it needs a running KukBook backed by an ISOLATED,
synthetic database (never production):

    KUKHIRE_REAL_HRMS_URL   e.g. http://127.0.0.1:41234     (host[:port] is allow-listed for the test)
    KUKHIRE_REAL_HRMS_KEY   a company API key with scope hr:write (and hr:read) on that server

See docs/HRMS_HANDOFF.md. The KukBook-side harness used to run this is described in KukBook's
docs/architecture/KUKHIRE_SEPARATION.md section 9.
"""
from __future__ import annotations

import os
from urllib.parse import urlparse

import httpx
import pytest

from tests.conftest import auth

URL = os.environ.get("KUKHIRE_REAL_HRMS_URL", "").rstrip("/")
KEY = os.environ.get("KUKHIRE_REAL_HRMS_KEY", "")

pytestmark = pytest.mark.skipif(not (URL and KEY), reason="KUKHIRE_REAL_HRMS_URL / KUKHIRE_REAL_HRMS_KEY not set")


@pytest.fixture()
def settings(settings):
    return settings.model_copy(update={"hrms_allowed_hosts_csv": urlparse(URL).netloc, "hrms_allow_insecure_http": True})


def _hired(client, ws, job_factory, application_factory, email):
    ws.user("owner")
    org = ws.create_org("owner")
    h = ws.h("owner")
    job = job_factory(org, h, title="Synthetic Engineer", department="Platform")
    app = application_factory(org, h, job["id"], email=email, name="Real Hrms Synthetic")
    for stage in ("screening", "interview", "offer", "hired"):
        assert client.post(f"/v1/organizations/{org}/applications/{app['id']}/transitions", json={"to_stage": stage}, headers=h).status_code == 200
    r = client.put(f"/v1/organizations/{org}/integrations/hrms", json={"hrms_base_url": URL, "api_key": KEY}, headers=h)
    assert r.status_code == 200, r.text
    return org, app, h


def test_handoff_creates_one_employee_and_replays(client, ws, job_factory, application_factory):
    org, app, h = _hired(client, ws, job_factory, application_factory, "real.hrms.1@example.com")
    first = client.post(f"/v1/organizations/{org}/applications/{app['id']}/handoff", json={}, headers=h)
    assert first.status_code in (200, 201), first.text
    body = first.json()
    assert body["status"] == "completed" and body["employee_ref"]

    again = client.post(f"/v1/organizations/{org}/applications/{app['id']}/handoff", json={}, headers=h)
    assert again.status_code in (200, 201), again.text
    assert again.json()["employee_ref"] == body["employee_ref"]  # same employee, not a second one

    # Reconcile directly with the real server: the claim exists and points at that employee.
    ref = f"kukhire:{app['id']}"
    got = httpx.get(f"{URL}/api/v1/hr/hires/{ref}", headers={"Authorization": f"Bearer {KEY}"}, timeout=10)
    assert got.status_code == 200, got.text
    assert str((got.json().get("data") or got.json())["employeeId"]) == body["employee_ref"]


def test_bad_key_is_reported_as_rejected_not_completed(client, ws, job_factory, application_factory):
    org, app, h = _hired(client, ws, job_factory, application_factory, "real.hrms.2@example.com")
    r = client.put(f"/v1/organizations/{org}/integrations/hrms", json={"hrms_base_url": URL, "api_key": "kb_live_not_a_real_key_000"}, headers=h)
    assert r.status_code == 200
    res = client.post(f"/v1/organizations/{org}/applications/{app['id']}/handoff", json={}, headers=h)
    assert res.status_code in (422, 502), res.text
    state = client.get(f"/v1/organizations/{org}/applications/{app['id']}/handoff", headers=h).json()["handoff"]
    assert state["status"] == "failed" and not state.get("employee_ref")
