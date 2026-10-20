"""Workspace isolation and role enforcement.

The shape of these tests: build two workspaces with real data, then try to reach A's records as a
member of B — by every route and every id type — and as users with weaker roles.
"""
from __future__ import annotations

import pytest

from tests.conftest import auth


@pytest.fixture()
def two_orgs(ws, job_factory, application_factory, client):
    ws.user("alice"); ws.user("bob")
    a = ws.create_org("alice", "Org A")
    b = ws.create_org("bob", "Org B")
    job_a = job_factory(a, ws.h("alice"))
    app_a = application_factory(a, ws.h("alice"), job_a["id"])
    sc = client.post(f"/v1/organizations/{a}/applications/{app_a['id']}/scorecards", json={"overall": 4}, headers=ws.h("alice"))
    assert sc.status_code == 201
    return {"a": a, "b": b, "job": job_a, "app": app_a, "candidate_id": app_a["candidate_id"]}


class TestCrossTenantAccess:
    def test_non_member_gets_404_for_the_whole_workspace(self, client, ws, two_orgs):
        a = two_orgs["a"]
        for path in ("", "/jobs", "/candidates", "/applications", "/stats", "/members", "/audit", "/integrations/hrms"):
            r = client.get(f"/v1/organizations/{a}{path}", headers=ws.h("bob"))
            assert r.status_code == 404, f"{path} leaked: {r.status_code}"
            assert r.json()["detail"]["error"] == "not_found"

    def test_unknown_workspace_is_indistinguishable_from_someone_elses(self, client, ws, two_orgs):
        missing = client.get("/v1/organizations/does-not-exist/jobs", headers=ws.h("bob"))
        theirs = client.get(f"/v1/organizations/{two_orgs['a']}/jobs", headers=ws.h("bob"))
        assert (missing.status_code, missing.json()) == (theirs.status_code, theirs.json())

    def test_ids_from_another_workspace_are_not_found_inside_my_own(self, client, ws, two_orgs):
        """Bob IS a member of B. Naming A's record ids inside B's URL must not reach them."""
        b, job, app, cand = two_orgs["b"], two_orgs["job"], two_orgs["app"], two_orgs["candidate_id"]
        hb = ws.h("bob")
        base = f"/v1/organizations/{b}"
        assert client.get(f"{base}/jobs/{job['id']}", headers=hb).status_code == 404
        assert client.patch(f"{base}/jobs/{job['id']}", json={"title": "pwned"}, headers=hb).status_code == 404
        assert client.delete(f"{base}/jobs/{job['id']}", headers=hb).status_code == 404
        assert client.get(f"{base}/candidates/{cand}", headers=hb).status_code == 404
        assert client.patch(f"{base}/candidates/{cand}", json={"full_name": "pwned"}, headers=hb).status_code == 404
        assert client.delete(f"{base}/candidates/{cand}", headers=hb).status_code == 404
        for sub in ("", "/handoff", "/scorecards", "/evaluations"):
            assert client.get(f"{base}/applications/{app['id']}{sub}", headers=hb).status_code == 404, sub
        assert client.post(f"{base}/applications/{app['id']}/transitions", json={"to_stage": "screening"}, headers=hb).status_code == 404
        assert client.post(f"{base}/applications/{app['id']}/scorecards", json={"overall": 5}, headers=hb).status_code == 404
        assert client.post(f"{base}/applications/{app['id']}/handoff", json={}, headers=hb).status_code == 404
        assert client.post(f"{base}/applications/{app['id']}/evaluations", headers=hb).status_code == 404

    def test_cannot_attach_a_foreign_job_or_candidate_when_creating(self, client, ws, two_orgs):
        b, job, cand = two_orgs["b"], two_orgs["job"], two_orgs["candidate_id"]
        hb = ws.h("bob")
        mine = client.post(f"/v1/organizations/{b}/jobs", json={"title": "Mine", "description": "d", "status": "open"}, headers=hb).json()
        r = client.post(f"/v1/organizations/{b}/applications", json={"job_id": job["id"], "candidate": {"full_name": "X", "email": "x@example.com"}}, headers=hb)
        assert r.status_code == 404  # A's job
        r = client.post(f"/v1/organizations/{b}/applications", json={"job_id": mine["id"], "candidate_id": cand}, headers=hb)
        assert r.status_code == 404  # A's candidate

    def test_lists_never_contain_the_other_workspaces_rows(self, client, ws, two_orgs):
        b = two_orgs["b"]
        for path in ("/jobs", "/candidates", "/applications"):
            assert client.get(f"/v1/organizations/{b}{path}", headers=ws.h("bob")).json()["items"] == []
        assert client.get(f"/v1/organizations/{b}/stats", headers=ws.h("bob")).json()["by_stage"]["applied"] == 0

    def test_same_email_in_two_workspaces_is_two_candidates(self, client, ws, two_orgs, job_factory, application_factory):
        b = two_orgs["b"]
        job_b = job_factory(b, ws.h("bob"))
        app_b = application_factory(b, ws.h("bob"), job_b["id"], email="cand@example.com")  # same as A's candidate
        assert app_b["candidate_id"] != two_orgs["candidate_id"]
        mine = client.get(f"/v1/organizations/{b}/candidates", headers=ws.h("bob")).json()
        assert mine["total"] == 1

    def test_membership_is_user_specific_not_email_domain_or_name_based(self, client, ws, two_orgs):
        ws.user("mallory", email="alice@example.com", verified=False)  # claims Alice's email, unverified
        r = client.get(f"/v1/organizations/{two_orgs['a']}", headers=ws.h("mallory"))
        assert r.status_code == 404


class TestInvitations:
    def test_invitation_is_claimed_only_by_a_verified_matching_email(self, client, ws, two_orgs):
        a = two_orgs["a"]
        ws.user("carol", email="carol@example.com", verified=False)
        r = client.post(f"/v1/organizations/{a}/members", json={"email": "carol@example.com", "role": "recruiter"}, headers=ws.h("alice"))
        assert r.status_code == 201 and r.json()["status"] == "invited"
        # Unverified email: cannot claim, cannot see the workspace.
        assert client.get("/v1/me", headers=ws.h("carol")).json()["organizations"] == []
        assert client.get(f"/v1/organizations/{a}", headers=ws.h("carol")).status_code == 404
        # The same address, verified: claims it.
        ws.user("carol", email="carol@example.com", verified=True)
        assert [o["role"] for o in client.get("/v1/me", headers=ws.h("carol")).json()["organizations"]] == ["recruiter"]
        assert client.get(f"/v1/organizations/{a}", headers=ws.h("carol")).status_code == 200

    def test_duplicate_invitation_is_refused(self, client, ws, two_orgs):
        a = two_orgs["a"]
        body = {"email": "dave@example.com", "role": "viewer"}
        assert client.post(f"/v1/organizations/{a}/members", json=body, headers=ws.h("alice")).status_code == 201
        r = client.post(f"/v1/organizations/{a}/members", json={**body, "email": "DAVE@example.com"}, headers=ws.h("alice"))
        assert r.status_code == 409 and r.json()["detail"]["error"] == "already_member"

    def test_last_admin_cannot_be_demoted_or_removed(self, client, ws, two_orgs):
        a = two_orgs["a"]
        members = client.get(f"/v1/organizations/{a}/members", headers=ws.h("alice")).json()["items"]
        me = members[0]["id"]
        r = client.patch(f"/v1/organizations/{a}/members/{me}", json={"role": "viewer"}, headers=ws.h("alice"))
        assert r.status_code == 409 and r.json()["detail"]["error"] == "last_admin"
        assert client.delete(f"/v1/organizations/{a}/members/{me}", headers=ws.h("alice")).status_code == 409

    def test_admin_can_manage_members_when_another_admin_exists(self, client, ws, two_orgs):
        a = two_orgs["a"]
        ws.user("erin")
        ws.add_member(a, "alice", "erin", "admin")
        members = client.get(f"/v1/organizations/{a}/members", headers=ws.h("alice")).json()["items"]
        erin = next(m for m in members if m["email"] == "erin@example.com")
        assert client.patch(f"/v1/organizations/{a}/members/{erin['id']}", json={"role": "viewer"}, headers=ws.h("alice")).status_code == 200
        assert client.delete(f"/v1/organizations/{a}/members/{erin['id']}", headers=ws.h("alice")).status_code == 204
        assert client.get(f"/v1/organizations/{a}", headers=ws.h("erin")).status_code == 404  # access ends at once


MATRIX = {
    # capability -> (method, path-template, body, roles that may)
    "read jobs": ("GET", "/jobs", None, {"admin", "recruiter", "interviewer", "viewer"}),
    "create job": ("POST", "/jobs", {"title": "T", "description": "d"}, {"admin", "recruiter"}),
    "list members": ("GET", "/members", None, {"admin"}),
    "invite member": ("POST", "/members", {"email": "new@example.com", "role": "viewer"}, {"admin"}),
    "read audit": ("GET", "/audit", None, {"admin"}),
    "hrms link": ("GET", "/integrations/hrms", None, {"admin"}),
    "create candidate": ("POST", "/candidates", {"full_name": "N"}, {"admin", "recruiter"}),
    "read candidates": ("GET", "/candidates", None, {"admin", "recruiter", "interviewer", "viewer"}),
}


@pytest.mark.parametrize("role", ["admin", "recruiter", "interviewer", "viewer"])
@pytest.mark.parametrize("name", sorted(MATRIX))
def test_role_matrix(client, ws, name, role):
    ws.user("owner"); ws.user("tester")
    org = ws.create_org("owner")
    if role != "admin":
        ws.add_member(org, "owner", "tester", role)
        who = "tester"
    else:
        who = "owner"
    method, path, body, allowed = MATRIX[name]
    r = client.request(method, f"/v1/organizations/{org}{path}", json=body, headers=ws.h(who))
    if role in allowed:
        assert r.status_code < 300, f"{role} should be allowed to {name}: {r.status_code} {r.text}"
    else:
        assert r.status_code == 403, f"{role} must NOT {name}: {r.status_code}"


class TestApplicationRoles:
    def _setup(self, ws, job_factory, application_factory):
        ws.user("owner"); ws.user("rec"); ws.user("intA"); ws.user("intB"); ws.user("view")
        org = ws.create_org("owner")
        for who, role in (("rec", "recruiter"), ("intA", "interviewer"), ("intB", "interviewer"), ("view", "viewer")):
            ws.add_member(org, "owner", who, role)
        job = job_factory(org, ws.h("rec"))
        app = application_factory(org, ws.h("rec"), job["id"])
        return org, app

    def test_only_recruiting_roles_write_applications_and_move_stages(self, client, ws, job_factory, application_factory):
        org, app = self._setup(ws, job_factory, application_factory)
        base = f"/v1/organizations/{org}/applications/{app['id']}"
        for who in ("intA", "view"):
            assert client.patch(base, json={"rating": 3}, headers=ws.h(who)).status_code == 403
            assert client.post(f"{base}/transitions", json={"to_stage": "screening"}, headers=ws.h(who)).status_code == 403
            assert client.post(f"{base}/handoff", json={}, headers=ws.h(who)).status_code == 403
            assert client.post(f"{base}/evaluations", headers=ws.h(who)).status_code == 403
        assert client.patch(base, json={"rating": 3}, headers=ws.h("rec")).status_code == 200

    def test_interviewers_see_only_their_own_scorecards_and_viewers_none(self, client, ws, job_factory, application_factory):
        org, app = self._setup(ws, job_factory, application_factory)
        base = f"/v1/organizations/{org}/applications/{app['id']}/scorecards"
        assert client.post(base, json={"overall": 5, "notes": "A's view"}, headers=ws.h("intA")).status_code == 201
        assert client.post(base, json={"overall": 2, "notes": "B's view"}, headers=ws.h("intB")).status_code == 201
        mine = client.get(base, headers=ws.h("intA")).json()["items"]
        assert [s["notes"] for s in mine] == ["A's view"] and mine[0]["mine"] is True
        assert len(client.get(base, headers=ws.h("rec")).json()["items"]) == 2
        assert client.get(base, headers=ws.h("view")).status_code == 403
        assert client.post(base, json={"overall": 3}, headers=ws.h("view")).status_code == 403
