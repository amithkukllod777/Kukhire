"""Jobs, candidates, applications, pipeline transitions, scorecards, search, erasure."""
from __future__ import annotations

import pytest


@pytest.fixture()
def org(ws):
    ws.user("owner")
    return ws.create_org("owner")


def base(org):
    return f"/v1/organizations/{org}"


class TestJobs:
    def test_create_read_update_and_list(self, client, ws, org, job_factory):
        h = ws.h("owner")
        job = job_factory(org, h)
        assert job["status"] == "open" and job["applications_count"] == 0
        assert client.get(f"{base(org)}/jobs/{job['id']}", headers=h).json()["title"] == "Backend Engineer"
        r = client.patch(f"{base(org)}/jobs/{job['id']}", json={"status": "on_hold", "openings": 3}, headers=h)
        assert r.json()["status"] == "on_hold" and r.json()["openings"] == 3
        assert [j["id"] for j in client.get(f"{base(org)}/jobs?status=on_hold", headers=h).json()["items"]] == [job["id"]]
        assert client.get(f"{base(org)}/jobs?status=open", headers=h).json()["items"] == []
        assert client.get(f"{base(org)}/jobs?q=backend", headers=h).json()["items"][0]["id"] == job["id"]

    def test_scoring_criteria_are_validated_on_create_and_update(self, client, ws, org):
        h = ws.h("owner")
        bad = {"title": "T", "description": "d", "scoring_criteria": [{"key": "BAD", "label": "x", "weight": 5}]}
        r = client.post(f"{base(org)}/jobs", json=bad, headers=h)
        assert r.status_code == 422 and r.json()["detail"]["error"] == "invalid_criteria"
        ok = client.post(f"{base(org)}/jobs", json={"title": "T", "description": "d"}, headers=h).json()
        r = client.patch(f"{base(org)}/jobs/{ok['id']}", json={"scoring_criteria": [{"key": "a", "label": "A", "weight": 0}]}, headers=h)
        assert r.status_code == 422

    def test_unknown_fields_are_rejected(self, client, ws, org):
        r = client.post(f"{base(org)}/jobs", json={"title": "T", "description": "d", "organization_id": "someone-else"}, headers=ws.h("owner"))
        assert r.status_code == 422

    def test_job_with_applications_cannot_be_deleted_but_empty_one_can(self, client, ws, org, job_factory, application_factory):
        h = ws.h("owner")
        busy, empty = job_factory(org, h), job_factory(org, h, title="Empty")
        application_factory(org, h, busy["id"])
        r = client.delete(f"{base(org)}/jobs/{busy['id']}", headers=h)
        assert r.status_code == 409 and r.json()["detail"]["error"] == "job_has_applications"
        assert client.delete(f"{base(org)}/jobs/{empty['id']}", headers=h).status_code == 204
        assert client.get(f"{base(org)}/jobs/{empty['id']}", headers=h).status_code == 404

    def test_closed_job_refuses_new_applications(self, client, ws, org, job_factory):
        h = ws.h("owner")
        job = job_factory(org, h, status="closed")
        r = client.post(f"{base(org)}/applications", json={"job_id": job["id"], "candidate": {"full_name": "A", "email": "a@example.com"}}, headers=h)
        assert r.status_code == 409 and r.json()["detail"]["error"] == "job_closed"


class TestCandidateIdentity:
    def test_same_email_applying_to_two_jobs_is_one_person_two_applications(self, client, ws, org, job_factory, application_factory):
        h = ws.h("owner")
        j1, j2 = job_factory(org, h), job_factory(org, h, title="Second")
        a1 = application_factory(org, h, j1["id"], email="Pat@Example.com", name="Pat")
        a2 = application_factory(org, h, j2["id"], email="pat@example.com", name="Pat B")
        assert a1["candidate_id"] == a2["candidate_id"] and a1["id"] != a2["id"]
        cand = client.get(f"{base(org)}/candidates/{a1['candidate_id']}", headers=h).json()
        assert cand["email"] == "pat@example.com" and len(cand["applications"]) == 2
        assert client.get(f"{base(org)}/candidates", headers=h).json()["total"] == 1

    def test_without_an_email_two_same_named_people_stay_separate(self, client, ws, org, job_factory):
        h = ws.h("owner")
        job = job_factory(org, h)
        ids = set()
        for _ in range(2):
            r = client.post(f"{base(org)}/applications", json={"job_id": job["id"], "candidate": {"full_name": "Sam Lee"}}, headers=h)
            assert r.status_code == 201
            ids.add(r.json()["candidate_id"])
        assert len(ids) == 2  # name alone is never an identity

    def test_double_application_to_the_same_job_is_refused(self, client, ws, org, job_factory, application_factory):
        h = ws.h("owner")
        job = job_factory(org, h)
        first = application_factory(org, h, job["id"])
        r = client.post(f"{base(org)}/applications", json={"job_id": job["id"], "candidate_id": first["candidate_id"]}, headers=h)
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "already_applied" and r.json()["detail"]["application_id"] == first["id"]

    def test_candidate_endpoint_refuses_a_duplicate_email(self, client, ws, org):
        h = ws.h("owner")
        assert client.post(f"{base(org)}/candidates", json={"full_name": "A", "email": "dup@example.com"}, headers=h).status_code == 201
        r = client.post(f"{base(org)}/candidates", json={"full_name": "B", "email": "DUP@example.com"}, headers=h)
        assert r.status_code == 409 and r.json()["detail"]["error"] == "duplicate_candidate"

    def test_resume_url_must_be_https_without_credentials(self, client, ws, org):
        h = ws.h("owner")
        for url in ("http://x.example.com/cv.pdf", "ftp://x.example.com/cv.pdf", "https://user:pw@x.example.com/cv.pdf", "javascript:alert(1)"):
            r = client.post(f"{base(org)}/candidates", json={"full_name": "A", "resume_url": url}, headers=h)
            assert r.status_code == 422, url
        ok = client.post(f"{base(org)}/candidates", json={"full_name": "A", "resume_url": "https://files.example.com/cv.pdf"}, headers=h)
        assert ok.status_code == 201 and ok.json()["resume_url"] == "https://files.example.com/cv.pdf"

    def test_a_referral_needs_a_referrer(self, client, ws, org, job_factory):
        h = ws.h("owner")
        job = job_factory(org, h)
        r = client.post(f"{base(org)}/applications", json={"job_id": job["id"], "candidate": {"full_name": "A"}, "source": "referral"}, headers=h)
        assert r.status_code == 422
        r = client.post(f"{base(org)}/applications", json={"job_id": job["id"], "candidate": {"full_name": "A"}, "source": "referral", "referrer_employee_ref": "17"}, headers=h)
        assert r.status_code == 201 and r.json()["referrer_employee_ref"] == "17"


class TestPipeline:
    def test_full_path_records_history_and_audit(self, client, ws, org, job_factory, application_factory):
        h = ws.h("owner")
        app = application_factory(org, h, job_factory(org, h)["id"])
        path = base(org) + f"/applications/{app['id']}"
        for stage in ("screening", "interview", "offer", "hired"):
            r = client.post(f"{path}/transitions", json={"to_stage": stage}, headers=h)
            assert r.status_code == 200 and r.json()["stage"] == stage, r.text
        detail = client.get(path, headers=h).json()
        assert [x["to_stage"] for x in detail["history"]] == ["applied", "screening", "interview", "offer", "hired"]
        assert detail["allowed_transitions"] == []  # hired is final
        actions = [a["action"] for a in client.get(f"{base(org)}/audit", headers=h).json()["items"]]
        assert actions.count("application.stage_changed") == 4

    def test_invalid_transitions_are_422_and_change_nothing(self, client, ws, org, job_factory, application_factory):
        h = ws.h("owner")
        app = application_factory(org, h, job_factory(org, h)["id"])
        path = base(org) + f"/applications/{app['id']}"
        r = client.post(f"{path}/transitions", json={"to_stage": "hired"}, headers=h)
        assert r.status_code == 422 and r.json()["detail"]["error"] == "invalid_transition"
        assert client.post(f"{path}/transitions", json={"to_stage": "nonsense"}, headers=h).status_code == 422
        assert client.get(path, headers=h).json()["stage"] == "applied"

    def test_reject_then_reopen_requires_a_reason(self, client, ws, org, job_factory, application_factory):
        h = ws.h("owner")
        app = application_factory(org, h, job_factory(org, h)["id"])
        path = base(org) + f"/applications/{app['id']}/transitions"
        assert client.post(path, json={"to_stage": "rejected", "reason": "Not a fit"}, headers=h).status_code == 200
        assert client.post(path, json={"to_stage": "applied"}, headers=h).status_code == 422
        assert client.post(path, json={"to_stage": "applied", "reason": "Reconsidered"}, headers=h).json()["stage"] == "applied"

    def test_a_stale_transition_loses_instead_of_overwriting(self, client, ws, org, job_factory, application_factory, monkeypatch):
        """Another person moves the card AFTER this request validated its move but BEFORE it wrote.
        The write is compare-and-set on the validated stage, so it must lose (409), not overwrite."""
        from sqlalchemy import update

        import app.routes.ats as ats_routes
        from app.db_models import Application

        h = ws.h("owner")
        app = application_factory(org, h, job_factory(org, h)["id"])
        path = base(org) + f"/applications/{app['id']}/transitions"
        real = ats_routes.validate_transition
        session_factory = client.app.state.session_factory

        def validate_then_lose_the_race(from_stage, to_stage, *, reason=None):
            result = real(from_stage, to_stage, reason=reason)  # validated against 'applied'
            with session_factory() as other:  # ...and a colleague moves it before we write
                other.execute(update(Application).where(Application.id == app["id"]).values(stage="interview"))
                other.commit()
            return result

        monkeypatch.setattr(ats_routes, "validate_transition", validate_then_lose_the_race)
        r = client.post(path, json={"to_stage": "screening"}, headers=h)
        assert r.status_code == 409 and r.json()["detail"]["error"] == "stage_changed"
        monkeypatch.setattr(ats_routes, "validate_transition", real)
        detail = client.get(base(org) + f"/applications/{app['id']}", headers=h).json()
        assert detail["stage"] == "interview"  # the colleague's move stands
        assert [x["to_stage"] for x in detail["history"]] == ["applied"]  # and ours left no trace

    def test_listing_filters_search_and_pagination(self, client, ws, org, job_factory, application_factory):
        h = ws.h("owner")
        j1, j2 = job_factory(org, h), job_factory(org, h, title="Designer")
        a = application_factory(org, h, j1["id"], email="ann@example.com", name="Ann Archer")
        application_factory(org, h, j1["id"], email="bo@example.com", name="Bo Brown")
        application_factory(org, h, j2["id"], email="cy@example.com", name="Cy_Cole")
        client.post(f"{base(org)}/applications/{a['id']}/transitions", json={"to_stage": "screening"}, headers=h)
        lst = lambda q: client.get(f"{base(org)}/applications{q}", headers=h).json()
        assert lst("")["total"] == 3
        assert {i["candidate_name"] for i in lst(f"?job_id={j1['id']}")["items"]} == {"Ann Archer", "Bo Brown"}
        assert [i["candidate_name"] for i in lst("?stage=screening")["items"]] == ["Ann Archer"]
        assert [i["candidate_name"] for i in lst("?q=BROWN")["items"]] == ["Bo Brown"]
        assert [i["candidate_name"] for i in lst("?q=cy_")["items"]] == ["Cy_Cole"]
        assert lst("?q=%25")["items"] == []  # LIKE wildcards are escaped, not interpreted
        assert len(lst("?limit=2")["items"]) == 2 and lst("?limit=2&offset=2")["total"] == 3
        assert client.get(f"{base(org)}/applications?stage=bogus", headers=h).status_code == 422

    def test_stats_reflect_the_pipeline(self, client, ws, org, job_factory, application_factory):
        h = ws.h("owner")
        job = job_factory(org, h)
        a = application_factory(org, h, job["id"], email="s1@example.com")
        application_factory(org, h, job["id"], email="s2@example.com")
        client.post(f"{base(org)}/applications/{a['id']}/transitions", json={"to_stage": "rejected"}, headers=h)
        s = client.get(f"{base(org)}/stats", headers=h).json()
        assert s["by_stage"]["applied"] == 1 and s["by_stage"]["rejected"] == 1
        assert s["open_jobs"] == 1 and s["candidates"] == 2 and s["open_applications"] == 1


class TestScorecards:
    def test_create_validate_and_list(self, client, ws, org, job_factory, application_factory):
        h = ws.h("owner")
        app = application_factory(org, h, job_factory(org, h)["id"])
        path = base(org) + f"/applications/{app['id']}/scorecards"
        r = client.post(path, json={"overall": 4.5, "technical": 4, "culture": 5, "notes": "Strong", "interview_date": "2026-10-01"}, headers=h)
        assert r.status_code == 201 and r.json()["overall"] == 4.5
        for bad in ({"overall": 0}, {"overall": 6}, {"overall": 3.33}, {"overall": 3, "technical": 9}, {"overall": 3, "interview_date": "soon"}):
            assert client.post(path, json=bad, headers=h).status_code == 422, bad
        assert len(client.get(path, headers=h).json()["items"]) == 1
        # One decimal place is accepted (the legacy column was DECIMAL(3,1)).
        assert client.post(path, json={"overall": 3.7}, headers=h).status_code == 201

    def test_feedback_cannot_be_added_to_a_closed_application(self, client, ws, org, job_factory, application_factory):
        h = ws.h("owner")
        app = application_factory(org, h, job_factory(org, h)["id"])
        client.post(f"{base(org)}/applications/{app['id']}/transitions", json={"to_stage": "rejected"}, headers=h)
        r = client.post(f"{base(org)}/applications/{app['id']}/scorecards", json={"overall": 3}, headers=h)
        assert r.status_code == 409


class TestErasure:
    def test_erasing_a_candidate_removes_everything_hanging_off_them(self, client, ws, org, job_factory, application_factory):
        h = ws.h("owner")
        app = application_factory(org, h, job_factory(org, h)["id"])
        client.post(f"{base(org)}/applications/{app['id']}/scorecards", json={"overall": 4}, headers=h)
        assert client.delete(f"{base(org)}/candidates/{app['candidate_id']}", headers=h).status_code == 204
        assert client.get(f"{base(org)}/candidates/{app['candidate_id']}", headers=h).status_code == 404
        assert client.get(f"{base(org)}/applications/{app['id']}", headers=h).status_code == 404
        audit = client.get(f"{base(org)}/audit", headers=h).json()["items"]
        erased = next(a for a in audit if a["action"] == "candidate.erased")
        # The audit row records that it happened — and carries no personal data of the person.
        assert "cand@example.com" not in str(erased) and "Casey" not in str(erased)

    def test_only_admins_can_erase(self, client, ws, org, job_factory, application_factory):
        ws.user("rec")
        ws.add_member(org, "owner", "rec", "recruiter")
        app = application_factory(org, ws.h("owner"), job_factory(org, ws.h("owner"))["id"])
        assert client.delete(f"{base(org)}/candidates/{app['candidate_id']}", headers=ws.h("rec")).status_code == 403
