"""Hiring handoff to HRMS.

``FakeHrms`` is a test double that behaves like the real endpoint's CONTRACT (idempotent on
externalApplicationRef, 401 on a bad key, 422 on invalid data). The real endpoint is exercised
end-to-end separately (tests/e2e against a running KukBook).
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.hrms_client import HrmsClient, HandoffRejected, HandoffTransient, normalize_hrms_base_url

API_KEY = "kb_live_SECRETSECRETSECRET1234"


class FakeHrms:
    def __init__(self) -> None:
        self.by_ref: dict[str, dict] = {}
        self.requests: list[httpx.Request] = []
        self.mode = "ok"  # ok | timeout | 500 | 401 | 422 | 409 | redirect | garbage
        self.next_employee = 100

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.mode == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        if self.mode == "redirect":
            return httpx.Response(302, headers={"Location": "https://evil.example.com/steal"})
        if self.mode in ("500", "401", "422", "409"):
            return httpx.Response(int(self.mode), json={"error": f"simulated {self.mode}"})
        if self.mode == "garbage":
            return httpx.Response(200, json={"data": {"employeeId": "not-an-int"}})
        assert request.headers["authorization"] == f"Bearer {API_KEY}"
        body = json.loads(request.content)
        ref = body["externalApplicationRef"]
        if ref in self.by_ref:  # idempotent replay
            return httpx.Response(200, json={"data": {**self.by_ref[ref], "created": False}})
        self.next_employee += 1
        rec = {"handoffId": self.next_employee, "employeeId": self.next_employee, "empId": f"EMP-{self.next_employee}",
               "onboardingChecklistId": None, "referralBonusStatus": "none"}
        self.by_ref[ref] = rec
        return httpx.Response(201, json={"data": {**rec, "created": True}})


@pytest.fixture()
def hrms():
    return FakeHrms()


@pytest.fixture()
def hired(client, ws, hrms, make_app, job_factory, application_factory):
    """A workspace with a HIRED application, an HRMS link, and the app wired to FakeHrms."""
    factory = lambda url, key: HrmsClient(url, key, transport=httpx.MockTransport(hrms.handler))
    from fastapi.testclient import TestClient

    c = TestClient(make_app(hrms_client_factory=factory))
    ws.client = c
    ws.user("owner")
    org = ws.create_org("owner")
    h = ws.h("owner")
    job = job_factory(org, h)  # job_factory/application_factory use the original client; same DB
    app = application_factory(org, h, job["id"])
    for stage in ("screening", "interview", "offer", "hired"):
        assert c.post(f"/v1/organizations/{org}/applications/{app['id']}/transitions", json={"to_stage": stage}, headers=h).status_code == 200
    r = c.put(f"/v1/organizations/{org}/integrations/hrms", json={"hrms_base_url": "https://hrms.example.com", "api_key": API_KEY}, headers=h)
    assert r.status_code == 200, r.text
    return {"c": c, "org": org, "app": app, "h": h, "hrms": hrms}


def go(hired, **body):
    return hired["c"].post(f"/v1/organizations/{hired['org']}/applications/{hired['app']['id']}/handoff", json=body, headers=hired["h"])


class TestHandoffFlow:
    def test_success_records_the_employee_reference_and_sends_only_what_hrms_needs(self, hired):
        r = go(hired, start_date="2026-11-03")
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["status"] == "completed" and out["employee_ref"] == "101" and out["employee_code"] == "EMP-101"
        assert out["external_ref"] == f"kukhire:{hired['app']['id']}" and out["attempts"] == 1
        sent = json.loads(hired["hrms"].requests[-1].content)
        assert sent["name"] == "Casey Candidate" and sent["startDate"] == "2026-11-03"
        assert sent["externalApplicationRef"] == out["external_ref"]
        # No compensation, no resume content, no tenant id: HRMS owns salary and takes the company
        # from the API key.
        assert not {"salary", "expectedSalary", "companyId", "parsed_resume", "notes"} & set(sent)
        detail = hired["c"].get(f"/v1/organizations/{hired['org']}/applications/{hired['app']['id']}", headers=hired["h"]).json()
        assert detail["handoff"]["employee_ref"] == "101"

    def test_referral_attribution_is_sent_but_no_bonus_decision(self, hired, application_factory):
        ref_app = application_factory(hired["org"], hired["h"], hired["app"]["job_id"], email="referred@example.com",
                                      source="referral", referrer_employee_ref="57", referrer_name="Rita Referrer")
        c, org, h = hired["c"], hired["org"], hired["h"]
        for stage in ("offer", "hired"):
            c.post(f"/v1/organizations/{org}/applications/{ref_app['id']}/transitions", json={"to_stage": stage}, headers=h)
        assert c.post(f"/v1/organizations/{org}/applications/{ref_app['id']}/handoff", json={}, headers=h).status_code == 200
        sent = json.loads(hired["hrms"].requests[-1].content)
        assert sent["referrerEmployeeId"] == 57  # attribution: who referred
        assert not any("bonus" in k.lower() for k in sent)  # approval/payment is HRMS/payroll's call

    def test_requesting_again_does_not_call_hrms_again(self, hired):
        go(hired)
        calls = len(hired["hrms"].requests)
        again = go(hired)
        assert again.status_code == 200 and again.json()["employee_ref"] == "101"
        assert len(hired["hrms"].requests) == calls

    def test_only_a_hired_application_can_be_handed_off(self, client, ws, hired, application_factory):
        other = application_factory(hired["org"], hired["h"], hired["app"]["job_id"], email="not-hired@example.com")
        r = hired["c"].post(f"/v1/organizations/{hired['org']}/applications/{other['id']}/handoff", json={}, headers=hired["h"])
        assert r.status_code == 409 and r.json()["detail"]["error"] == "not_hired"
        assert hired["hrms"].requests == []

    def test_a_workspace_without_an_hrms_link_says_so(self, client, ws, job_factory, application_factory):
        ws.user("owner")
        org = ws.create_org("owner")
        h = ws.h("owner")
        app = application_factory(org, h, job_factory(org, h)["id"])
        for stage in ("offer", "hired"):
            client.post(f"/v1/organizations/{org}/applications/{app['id']}/transitions", json={"to_stage": stage}, headers=h)
        r = client.post(f"/v1/organizations/{org}/applications/{app['id']}/handoff", json={}, headers=h)
        assert r.status_code == 409 and r.json()["detail"]["error"] == "hrms_not_configured"


class TestFailureHandling:
    def test_unknown_outcome_is_recorded_as_retryable_and_a_retry_does_not_duplicate(self, hired):
        hrms = hired["hrms"]
        hrms.mode = "timeout"
        r = go(hired)
        assert r.status_code == 502 and r.json()["detail"]["error"] == "hrms_unavailable"
        state = hired["c"].get(f"/v1/organizations/{hired['org']}/applications/{hired['app']['id']}/handoff", headers=hired["h"]).json()["handoff"]
        assert state["status"] == "failed" and state["last_error"].startswith("retryable") and state["employee_ref"] is None
        hrms.mode = "ok"
        ok = go(hired)
        assert ok.status_code == 200 and ok.json()["status"] == "completed" and ok.json()["attempts"] == 2
        assert len(hrms.by_ref) == 1  # one employee, however many attempts

    def test_lost_response_after_hrms_created_the_employee_still_yields_exactly_one(self, hired):
        """HRMS commits, the response is lost (timeout). The retry sends the SAME reference."""
        hrms = hired["hrms"]
        real = hrms.handler

        def create_then_drop(request):
            real(request)  # HRMS did the work...
            raise httpx.ReadTimeout("response lost", request=request)  # ...KukHire never heard back

        hrms_handler_backup = hrms.handler
        hrms.handler = create_then_drop
        factory = lambda url, key: HrmsClient(url, key, transport=httpx.MockTransport(hrms.handler))
        hired["c"].app.state.hrms_client_factory = factory
        assert go(hired).status_code == 502
        hrms.handler = hrms_handler_backup
        hired["c"].app.state.hrms_client_factory = lambda url, key: HrmsClient(url, key, transport=httpx.MockTransport(hrms.handler))
        ok = go(hired)
        assert ok.status_code == 200 and ok.json()["employee_ref"] == "101"
        assert len(hrms.by_ref) == 1

    @pytest.mark.parametrize("mode,status,code", [("401", 422, "hrms_rejected"), ("422", 422, "hrms_rejected"),
                                                   ("500", 502, "hrms_unavailable"), ("409", 502, "hrms_unavailable"),
                                                   ("garbage", 502, "hrms_unavailable"), ("redirect", 422, "hrms_rejected")])
    def test_failure_modes_map_to_honest_statuses_and_never_to_success(self, hired, mode, status, code):
        hired["hrms"].mode = mode
        r = go(hired)
        assert (r.status_code, r.json()["detail"]["error"]) == (status, code)
        state = hired["c"].get(f"/v1/organizations/{hired['org']}/applications/{hired['app']['id']}/handoff", headers=hired["h"]).json()["handoff"]
        assert state["status"] == "failed" and state["employee_ref"] is None

    def test_the_api_key_never_appears_in_any_response_or_audit_row(self, hired):
        hired["hrms"].mode = "401"
        bodies = [go(hired).text]
        hired["hrms"].mode = "ok"
        bodies += [go(hired).text]
        c, org, h = hired["c"], hired["org"], hired["h"]
        bodies += [c.get(f"/v1/organizations/{org}/integrations/hrms", headers=h).text,
                   c.get(f"/v1/organizations/{org}/audit", headers=h).text,
                   c.get(f"/v1/organizations/{org}/applications/{hired['app']['id']}", headers=h).text]
        assert all("SECRETSECRET" not in b and API_KEY not in b for b in bodies)
        assert c.get(f"/v1/organizations/{org}/integrations/hrms", headers=h).json() == {
            "configured": True, "hrms_base_url": "https://hrms.example.com", "api_key_hint": "1234"}

    def test_stored_key_is_encrypted_at_rest(self, hired):
        from sqlalchemy import text

        with hired["c"].app.state.engine.connect() as conn:
            stored = conn.execute(text("SELECT encrypted_api_key FROM organization_integrations")).scalar()
        assert API_KEY not in stored and "SECRETSECRET" not in stored


class TestHrmsLink:
    URL = "https://hrms.example.com"

    def put(self, client, ws, org, **over):
        return client.put(f"/v1/organizations/{org}/integrations/hrms", json={"hrms_base_url": self.URL, "api_key": API_KEY, **over}, headers=ws.h("owner"))

    @pytest.fixture()
    def org(self, ws):
        ws.user("owner")
        return ws.create_org("owner")

    @pytest.mark.parametrize("url", [
        "http://hrms.example.com", "https://evil.example.com", "https://hrms.example.com:8443",
        "https://user:pw@hrms.example.com", "https://hrms.example.com/api", "https://hrms.example.com?x=1",
        "https://169.254.169.254", "https://localhost", "ftp://hrms.example.com", "hrms.example.com",
    ])
    def test_urls_outside_the_allowlist_or_with_extras_are_refused(self, client, ws, org, url):
        r = self.put(client, ws, org, hrms_base_url=url)
        assert r.status_code == 422 and r.json()["detail"]["error"] == "invalid_hrms_url", url

    def test_allowlisted_origin_is_normalized(self, client, ws, org):
        r = self.put(client, ws, org, hrms_base_url="https://HRMS.Example.com/")
        assert r.status_code == 200 and r.json()["hrms_base_url"] == "https://hrms.example.com"

    def test_only_admins_manage_the_link(self, client, ws, org):
        ws.user("rec")
        ws.add_member(org, "owner", "rec", "recruiter")
        r = client.put(f"/v1/organizations/{org}/integrations/hrms", json={"hrms_base_url": self.URL, "api_key": API_KEY}, headers=ws.h("rec"))
        assert r.status_code == 403

    def test_no_encryption_key_means_no_secret_is_stored(self, ws, make_app, settings, org):
        from fastapi.testclient import TestClient

        bare = settings.model_copy(update={"secret_encryption_key": ""})
        with TestClient(make_app(settings=bare)) as c:
            r = c.put(f"/v1/organizations/{org}/integrations/hrms", json={"hrms_base_url": self.URL, "api_key": API_KEY}, headers=ws.h("owner"))
            assert r.status_code == 503 and r.json()["detail"]["error"] == "secret_storage_unavailable"

    def test_removing_the_link_stops_handoff(self, client, ws, org):
        assert self.put(client, ws, org).status_code == 200
        assert client.delete(f"/v1/organizations/{org}/integrations/hrms", headers=ws.h("owner")).status_code == 204
        assert client.get(f"/v1/organizations/{org}/integrations/hrms", headers=ws.h("owner")).json() == {"configured": False}


class TestClientUnit:
    def test_redirects_are_not_followed_so_the_key_cannot_travel(self):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(307, headers={"Location": "https://evil.example.com/"})

        with pytest.raises(HandoffRejected, match="redirect"):
            HrmsClient("https://hrms.example.com", API_KEY, transport=httpx.MockTransport(handler)).create_hire({"externalApplicationRef": "x"})
        assert seen == ["https://hrms.example.com/api/v1/hr/hires"]

    def test_transport_errors_do_not_echo_the_request(self):
        def boom(request):
            raise httpx.ConnectError(f"cannot connect to {request.url} with {request.headers['authorization']}", request=request)

        with pytest.raises(HandoffTransient) as exc:
            HrmsClient("https://hrms.example.com", API_KEY, transport=httpx.MockTransport(boom)).create_hire({})
        assert API_KEY not in str(exc.value) and "hrms.example.com" not in str(exc.value)

    def test_normalize_requires_an_explicit_allowlist(self):
        with pytest.raises(ValueError):
            normalize_hrms_base_url("https://hrms.example.com", allowed_hosts=(), allow_insecure_http=False)
        assert normalize_hrms_base_url("http://127.0.0.1:9", allowed_hosts=("127.0.0.1:9",), allow_insecure_http=True) == "http://127.0.0.1:9"
