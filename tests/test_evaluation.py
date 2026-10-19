"""Advisory evaluation: honesty about availability, redaction, validation, no automated decisions."""
from __future__ import annotations

import json
import logging
import sys

import pytest
from fastapi.testclient import TestClient

from app.evaluation import redact_resume
from tests.conftest import CRITERIA

RESUME = {
    "basics": {
        "name": "Zelda Zephyr", "email": "zelda.zephyr@example.com", "phone": "+1 415 555 0199",
        "label": "Backend engineer", "summary": "Builds APIs. Reach me at zelda.zephyr@example.com or +1 415 555 0199.",
        "location": {"city": "Springfield", "countryCode": "US"},
        "url": "https://zelda.example.com", "profiles": [{"network": "github", "username": "zeldaz", "url": "https://github.com/zeldaz"}],
    },
    "work": [{"name": "Hyrule Labs", "position": "Senior Engineer", "startDate": "2019-03-01", "endDate": "2024-06-30",
              "summary": "Ran the payments platform.", "highlights": ["Cut p99 latency by 40%", "Mentored 4 engineers; see https://blog.example.com/x"]}],
    "education": [{"institution": "Hogwarts University", "area": "Computer Science", "studyType": "BSc", "score": "3.9 GPA",
                   "startDate": "2010-09-01", "endDate": "2014-06-01"}],
    "skills": [{"name": "Backend", "keywords": ["Python", "PostgreSQL"]}],
    "projects": [{"name": "Rate limiter", "description": "Token bucket service", "highlights": ["10k rps"], "url": "https://github.com/zeldaz/rl"}],
    "languages": [{"language": "Hylian", "fluency": "Native"}],
    "interests": [{"name": "Hiking"}],
    "references": [{"name": "Impa", "reference": "Great"}],
}
PII = ["Zelda", "Zephyr", "zelda.zephyr", "415 555", "Springfield", "Hogwarts", "3.9", "github.com/zeldaz", "zeldaz",
       "zelda.example.com", "blog.example.com", "Hylian", "Hiking", "Impa", "2010", "2014"]


class FakeProvider:
    name, model = "fake", "fake-model-1"

    def __init__(self, response=None, error=None):
        self.response, self.error = response, error
        self.system = self.user = self.schema = None

    def generate(self, system, user, schema):
        self.system, self.user, self.schema = system, user, schema
        if self.error:
            raise self.error
        return self.response if isinstance(self.response, str) else json.dumps(self.response)


def good_output(**over):
    out = {"criteria": [
        {"key": "python", "score": 9, "evidence": "Led a Python payments platform.", "missing_information": []},
        {"key": "communication", "score": 5, "evidence": "Mentored four engineers.", "missing_information": ["public speaking"]},
    ], "summary": "Strong backend profile.", "risks": ["Limited front-end exposure"]}
    out.update(over)
    return out


@pytest.fixture()
def candidate_app(client, ws, job_factory):
    ws.user("owner")
    org = ws.create_org("owner")
    h = ws.h("owner")
    job = job_factory(org, h)
    r = client.post(f"/v1/organizations/{org}/applications", headers=h, json={
        "job_id": job["id"], "candidate": {"full_name": "Zelda Zephyr", "email": "zelda.zephyr@example.com",
                                           "phone": "+1 415 555 0199", "parsed_resume": RESUME}})
    assert r.status_code == 201, r.text
    return {"org": org, "h": h, "app": r.json(), "job": job}


def enabled_client(make_app, settings, provider):
    cfg = settings.model_copy(update={"evaluation_enabled": True})
    return TestClient(make_app(settings=cfg, evaluation_provider_factory=lambda: provider))


def run(c, ctx):
    return c.post(f"/v1/organizations/{ctx['org']}/applications/{ctx['app']['id']}/evaluations", headers=ctx["h"])


class TestAvailability:
    def test_disabled_deployment_answers_explicitly_and_records_nothing(self, client, candidate_app):
        r = run(client, candidate_app)
        assert r.status_code == 503
        assert r.json()["detail"]["error"] == "evaluation_not_enabled"
        assert "queued" not in r.text.lower()
        lst = client.get(f"/v1/organizations/{candidate_app['org']}/applications/{candidate_app['app']['id']}/evaluations", headers=candidate_app["h"])
        assert lst.json()["items"] == []

    def test_capabilities_do_not_claim_what_is_not_there(self, client):
        caps = {c["key"]: c for c in client.get("/v1/capabilities").json()["capabilities"]}
        assert caps["ai_evaluation"]["status"] == "unavailable"
        assert caps["email_ingestion"]["status"] == "unavailable" and caps["resume_upload_parsing"]["status"] == "unavailable"

    def test_unsupported_provider_is_refused_not_silently_swapped(self, make_app, settings, candidate_app):
        cfg = settings.model_copy(update={"evaluation_enabled": True, "evaluation_provider": "gemini"})
        with TestClient(make_app(settings=cfg)) as c:
            r = run(c, candidate_app)
            assert r.status_code == 503 and r.json()["detail"]["error"] == "provider_unsupported"

    def test_missing_local_runtime_is_reported_as_unavailable(self, make_app, settings, candidate_app, monkeypatch):
        monkeypatch.setitem(sys.modules, "ollama", None)  # makes `import ollama` raise ImportError
        cfg = settings.model_copy(update={"evaluation_enabled": True})
        with TestClient(make_app(settings=cfg)) as c:
            r = run(c, candidate_app)
            assert r.status_code == 503 and r.json()["detail"]["error"] == "provider_unavailable"


class TestSuccessIsAdvisory:
    def test_scores_are_stored_with_evidence_and_the_overall_is_computed_by_us(self, make_app, settings, candidate_app):
        provider = FakeProvider(good_output(overall=10, overall_score=100))  # model-supplied totals must be ignored
        with enabled_client(make_app, settings, provider) as c:
            r = run(c, candidate_app)
            assert r.status_code == 201, r.text
            e = r.json()
        assert e["advisory"] is True and e["status"] == "completed"
        assert e["overall"] == 7.4  # (9*60 + 5*40) / 100, not whatever the model claimed
        assert [x["key"] for x in e["criteria"]] == ["python", "communication"]
        assert e["criteria"][0]["evidence"] and e["risks"] == ["Limited front-end exposure"]

    def test_an_evaluation_never_moves_an_application_or_sets_a_score(self, make_app, settings, candidate_app):
        with enabled_client(make_app, settings, FakeProvider(good_output())) as c:
            before = c.get(f"/v1/organizations/{candidate_app['org']}/applications/{candidate_app['app']['id']}", headers=candidate_app["h"]).json()
            assert run(c, candidate_app).status_code == 201
            after = c.get(f"/v1/organizations/{candidate_app['org']}/applications/{candidate_app['app']['id']}", headers=candidate_app["h"]).json()
        assert after["stage"] == before["stage"] == "applied"
        assert after["history"] == before["history"]
        with c.app.state.session_factory() as s:
            from app.db_models import Application
            assert s.get(Application, candidate_app["app"]["id"]).score is None

    def test_only_recruiting_roles_can_read_evaluations(self, make_app, settings, ws, candidate_app):
        ws.user("view")
        ws.add_member(candidate_app["org"], "owner", "view", "viewer")
        with enabled_client(make_app, settings, FakeProvider(good_output())) as c:
            run(c, candidate_app)
            base = f"/v1/organizations/{candidate_app['org']}/applications/{candidate_app['app']['id']}"
            assert c.get(f"{base}/evaluations", headers=ws.h("view")).status_code == 403
            assert "latest_evaluation" not in c.get(base, headers=ws.h("view")).json()
            assert c.get(base, headers=candidate_app["h"]).json()["latest_evaluation"]["advisory"] is True


class TestWhatTheModelIsToldAndSees:
    def test_prompt_is_job_specific_with_no_hardcoded_rubric(self, make_app, settings, candidate_app):
        provider = FakeProvider(good_output())
        with enabled_client(make_app, settings, provider) as c:
            run(c, candidate_app)
        assert "Backend Engineer" in provider.user and "Python depth" in provider.user and "weight 60" in provider.user
        for banned in ("HackerRank", "Software Intern", "GSoC", "Girl Script", "Hacktoberfest"):
            assert banned not in provider.system + provider.user
        assert "protected" in provider.system.lower() and "advisory" in provider.system.lower()
        assert set(provider.schema["properties"]) == {"criteria", "summary", "risks"}

    def test_no_identity_contact_or_proxy_details_reach_the_model(self, make_app, settings, candidate_app):
        provider = FakeProvider(good_output())
        with enabled_client(make_app, settings, provider) as c:
            run(c, candidate_app)
        sent = provider.system + provider.user
        leaked = [p for p in PII if p.lower() in sent.lower()]
        assert leaked == [], f"sent to the model: {leaked}"
        for useful in ("Senior Engineer", "payments platform", "Cut p99 latency", "PostgreSQL", "Computer Science", "Rate limiter", "2019-2024"):
            assert useful in sent, useful

    def test_redact_resume_unit(self):
        out = json.dumps(redact_resume(RESUME))
        assert not [p for p in PII if p.lower() in out.lower()]
        assert "[email]" in out and "[phone]" in out and "[link]" in out
        assert redact_resume(None) == {} and redact_resume({"basics": {"name": "X"}}) == {}


class TestRejectionOfBadModelOutput:
    @pytest.mark.parametrize("response,code", [
        ("not json at all", "invalid_model_output"),
        ({"criteria": "nope"}, "invalid_model_output"),
        (good_output(criteria=[{"key": "python", "score": 11, "evidence": "x", "missing_information": []},
                               {"key": "communication", "score": 5, "evidence": "x"}]), "invalid_model_output"),
        (good_output(criteria=[{"key": "python", "score": 5, "evidence": "  ", "missing_information": []},
                               {"key": "communication", "score": 5, "evidence": "x"}]), "invalid_model_output"),
        (good_output(criteria=[{"key": "python", "score": 5, "evidence": "x"}]), "criteria_mismatch"),
        (good_output(criteria=[{"key": "python", "score": 5, "evidence": "x"}, {"key": "communication", "score": 5, "evidence": "x"},
                               {"key": "invented", "score": 9, "evidence": "x"}]), "criteria_mismatch"),
        (good_output(criteria=[{"key": "python", "score": 5, "evidence": "x"}, {"key": "python", "score": 5, "evidence": "x"}]), "criteria_mismatch"),
    ])
    def test_invalid_answers_are_recorded_as_failures_with_no_score(self, make_app, settings, candidate_app, response, code):
        with enabled_client(make_app, settings, FakeProvider(response)) as c:
            r = run(c, candidate_app)
            assert r.status_code == 502 and r.json()["detail"]["error"] == code
            items = c.get(f"/v1/organizations/{candidate_app['org']}/applications/{candidate_app['app']['id']}/evaluations", headers=candidate_app["h"]).json()["items"]
        assert len(items) == 1 and items[0]["status"] == "failed" and items[0]["overall"] is None and items[0]["criteria"] == []

    def test_fenced_and_thinking_wrapped_json_is_accepted(self, make_app, settings, candidate_app):
        wrapped = "<think>hmm</think>\n```json\n" + json.dumps(good_output()) + "\n```"
        with enabled_client(make_app, settings, FakeProvider(wrapped)) as c:
            assert run(c, candidate_app).status_code == 201

    def test_provider_error_is_a_clean_502(self, make_app, settings, candidate_app):
        with enabled_client(make_app, settings, FakeProvider(error=RuntimeError("boom with Zelda Zephyr in it"))) as c:
            r = run(c, candidate_app)
            assert r.status_code == 502 and r.json()["detail"]["error"] == "provider_error"
            assert "Zelda" not in r.text


class TestPreconditions:
    def test_job_without_criteria_cannot_be_evaluated(self, make_app, settings, client, candidate_app):
        client.patch(f"/v1/organizations/{candidate_app['org']}/jobs/{candidate_app['job']['id']}", json={"scoring_criteria": []}, headers=candidate_app["h"])
        with enabled_client(make_app, settings, FakeProvider(good_output())) as c:
            r = run(c, candidate_app)
            assert r.status_code == 422 and r.json()["detail"]["error"] == "no_scoring_criteria"

    def test_candidate_without_resume_content_cannot_be_evaluated(self, make_app, settings, client, ws, job_factory, application_factory):
        ws.user("owner2")
        org = ws.create_org("owner2")
        h = ws.h("owner2")
        app = application_factory(org, h, job_factory(org, h)["id"])
        ctx = {"org": org, "h": h, "app": app}
        provider = FakeProvider(good_output())
        with enabled_client(make_app, settings, provider) as c:
            r = run(c, ctx)
            assert r.status_code == 422 and r.json()["detail"]["error"] == "no_resume_content"
        assert provider.user is None  # the model was never called


class TestLogsCarryNoCandidateData:
    @pytest.mark.parametrize("response", [good_output(), "garbage"])
    def test_nothing_from_the_resume_the_prompt_or_the_answer_is_logged(self, make_app, settings, candidate_app, caplog, response):
        caplog.set_level(logging.DEBUG)
        marker = "UNIQUE-MODEL-ANSWER-MARKER"
        resp = response if isinstance(response, str) else {**response, "summary": marker}
        with enabled_client(make_app, settings, FakeProvider(resp)) as c:
            run(c, candidate_app)
        logged = "\n".join(r.getMessage() for r in caplog.records)
        for secret in PII + [marker, "Cut p99", "payments platform", "garbage"]:
            assert secret.lower() not in logged.lower(), f"{secret!r} was logged"
        assert "evaluation" in logged  # it did log metadata
