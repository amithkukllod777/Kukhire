"""Browser journeys. After each UI action the test checks the API (persistence) and reloads."""
from __future__ import annotations

import pytest

from tests.e2e.conftest import HRMS_KEY, api, create_workspace, sign_in, token

pytestmark = pytest.mark.e2e


def org_id(live, page):
    return api(live, page, "GET", "/v1/me").json()["organizations"][0]["id"]


def add_job(page, title="Backend Engineer"):
    page.click("[data-nav=jobs]")
    page.click("summary:has-text('New job')")
    page.fill("#job-title", title)
    page.fill("#job-description", "Build and run services.")
    page.select_option("#job-status", "open")
    page.fill("#criteria [data-f=key]", "python")
    page.fill("#criteria [data-f=label]", "Python depth")
    page.fill("#criteria [data-f=weight]", "60")
    page.click("#job-form button[type=submit]")
    page.wait_for_selector(f"tr[data-job] strong:has-text('{title}')")


def add_application(page, name="Casey Candidate", email="casey@example.com", **extra):
    page.click("[data-nav=candidates]")
    page.click("summary:has-text('Add application')")
    page.fill("#app-name", name)
    page.fill("#app-email", email)
    for sel, val in extra.items():
        page.fill(sel, val)
    page.click("#app-form button[type=submit]")
    page.wait_for_selector(f"tr[data-candidate] td:has-text('{name}')")


class TestSignIn:
    def test_wrong_password_shows_an_error_and_stays_signed_out(self, live, page):
        sign_in(page, live, password="nope")
        page.wait_for_selector(".error:has-text('Invalid credentials')")
        assert page.evaluate("sessionStorage.getItem('kukhire.session')") is None

    def test_otp_step_then_workspace_picker(self, live, page):
        page.goto(live.kuk + "/")
        page.fill("#identifier", "otto@example.com"); page.fill("#password", "pw-otto"); page.click("button[type=submit]")
        page.wait_for_selector("#otp")
        page.fill("#otp", "000000"); page.click("button[type=submit]")
        page.wait_for_selector(".error:has-text('Invalid code')")
        page.fill("#otp", "123456"); page.click("button[type=submit]")
        page.wait_for_selector("#create-org")  # signed in; no workspace yet

    def test_session_survives_reload_and_signing_out_clears_it(self, live, page):
        create_workspace(page, live)
        page.reload()
        page.wait_for_selector("#role-badge")
        page.click("text=Sign out")
        page.wait_for_selector("#identifier")
        assert page.evaluate("sessionStorage.getItem('kukhire.session')") is None
        assert page.evaluate("document.body.innerText").count("Acme Hiring") == 0

    def test_an_expired_access_token_is_refreshed_silently(self, live, page):
        create_workspace(page, live)
        live.auth.expire_access_tokens()  # the identity server forgets the access token
        page.click("[data-nav=jobs]")  # next API call gets 401 → UI refreshes → retries
        page.wait_for_selector("h1:has-text('Jobs')")
        assert not page.query_selector("#identifier")

    def test_a_dead_session_returns_to_sign_in(self, live, page):
        create_workspace(page, live)
        live.auth.expire_access_tokens(); live.auth.refresh.clear()
        page.click("[data-nav=jobs]")
        page.wait_for_selector("#identifier")


class TestRecruiterJourney:
    def test_job_application_stage_scorecard_and_hrms_handoff(self, live, page):
        create_workspace(page, live)
        oid = org_id(live, page)

        # --- job (with criteria) ---
        add_job(page)
        jobs = api(live, page, "GET", f"/v1/organizations/{oid}/jobs").json()["items"]
        assert [j["title"] for j in jobs] == ["Backend Engineer"] and jobs[0]["status"] == "open"
        assert jobs[0]["scoring_criteria"][0]["key"] == "python"

        # --- application via the candidates form ---
        add_application(page)
        page.click("[data-nav=pipeline]")
        page.wait_for_selector("[data-col=applied] [data-app]:has-text('Casey Candidate')")
        apps = api(live, page, "GET", f"/v1/organizations/{oid}/applications").json()["items"]
        assert len(apps) == 1 and apps[0]["stage"] == "applied"

        # --- move through the pipeline; each move is persisted and survives a reload ---
        page.click("[data-app]")
        page.select_option("#move-to", "screening")
        page.click("#move-btn")
        page.wait_for_selector("[data-col=screening] [data-app]")
        assert api(live, page, "GET", f"/v1/organizations/{oid}/applications").json()["items"][0]["stage"] == "screening"
        page.reload()
        page.wait_for_selector("[data-col=screening] [data-app]:has-text('Casey Candidate')")

        # an invalid move is not offered by the UI, and is refused by the API even if forced
        page.click("[data-app]")
        offered = page.eval_on_selector_all("#move-to option", "els => els.map(e => e.value)")
        assert "hired" not in offered and "screening" not in offered
        forced = api(live, page, "POST", f"/v1/organizations/{oid}/applications/{apps[0]['id']}/transitions", json={"to_stage": "hired"})
        assert forced.status_code == 422

        # --- scorecard ---
        page.select_option("#sc-overall", "4.5")
        page.fill("#sc-notes", "Strong systems thinking.")
        page.click("#scorecard-form button[type=submit]")
        page.wait_for_selector("[data-scorecard]:has-text('4.5/5')")
        cards = api(live, page, "GET", f"/v1/organizations/{oid}/applications/{apps[0]['id']}/scorecards").json()["items"]
        assert cards[0]["notes"] == "Strong systems thinking."

        # --- to offer, then hired ---
        for stage in ("offer", "hired"):
            page.select_option("#move-to", stage)
            page.click("#move-btn")
            page.wait_for_selector(f"[data-col={stage}] [data-app]")  # the drawer re-opens itself with the new state
            page.wait_for_function("(s) => document.querySelector('#drawer .badge')?.textContent.toLowerCase() === s", arg=stage)
        assert page.query_selector("#move-btn") is None  # hired is final

        # --- HRMS not connected yet: the UI says so and the API refuses ---
        assert page.query_selector("#handoff-btn")
        page.click("#handoff-btn")
        page.wait_for_selector(".toast.err:has-text('not linked')")
        assert live.hrms.requests == []
        page.click("#close-drawer")

        # --- connect HRMS from Settings (admin) ---
        page.click("[data-nav=settings]")
        page.fill("#hrms-url", live.hrms_url); page.fill("#hrms-key", HRMS_KEY)
        page.click("#hrms-form button[type=submit]")
        page.wait_for_selector("#hrms-state:has-text('Connected')")
        assert HRMS_KEY not in page.content()  # the key is never echoed back

        # --- hand off ---
        page.click("[data-nav=pipeline]")
        page.click("[data-col=hired] [data-app]")
        page.click("#handoff-btn")
        page.wait_for_selector("#handoff-status:has-text('completed')")
        assert page.inner_text("#handoff-status").count("EMP-501") == 1
        assert len(live.hrms.requests) == 1 and live.hrms.requests[0]["name"] == "Casey Candidate"
        assert "salary" not in live.hrms.requests[0]
        detail = api(live, page, "GET", f"/v1/organizations/{oid}/applications/{apps[0]['id']}").json()
        assert detail["handoff"]["employee_ref"] == "501" and detail["handoff"]["status"] == "completed"
        assert page.query_selector("#handoff-btn") is None  # nothing left to click: no duplicate possible
        assert page.errors == []

    def test_referral_attribution_and_pipeline_filter(self, live, page):
        create_workspace(page, live)
        add_job(page, "Designer")
        page.click("[data-nav=candidates]")
        page.click("summary:has-text('Add application')")
        page.fill("#app-name", "Riya Referred"); page.fill("#app-email", "riya@example.com")
        page.select_option("#app-source", "referral"); page.fill("#app-referrer", "57")
        page.click("#app-form button[type=submit]")
        page.wait_for_selector("tr[data-candidate]")
        oid = org_id(live, page)
        app = api(live, page, "GET", f"/v1/organizations/{oid}/applications").json()["items"][0]
        assert app["source"] == "referral" and app["referrer_employee_ref"] == "57"
        page.click("[data-nav=pipeline]")
        page.select_option("#job-filter", label="Designer")
        page.wait_for_selector("[data-app]:has-text('Riya Referred')")

    def test_candidate_data_is_rendered_as_text_never_as_html(self, live, page):
        create_workspace(page, live)
        add_job(page)
        add_application(page, name="<img src=x onerror=window.__pwned=1>", email="xss@example.com")
        page.click("[data-nav=pipeline]")
        page.wait_for_selector("[data-app]")
        assert "<img src=x" in page.inner_text("[data-app]")
        assert page.evaluate("window.__pwned") is None
        assert page.query_selector("[data-app] img") is None


class TestRolesInTheBrowser:
    def test_invited_viewer_sees_no_controls_and_the_api_agrees(self, live, page, browser):
        create_workspace(page, live)
        add_job(page)
        add_application(page)
        oid = org_id(live, page)
        # Alice invites Vera as a viewer through the UI.
        page.click("[data-nav=settings]")
        page.fill("#invite-email", "vera@example.com"); page.select_option("#invite-role", "viewer")
        page.click("#invite-form button[type=submit]")
        page.wait_for_selector("[data-member]:has-text('vera@example.com')")

        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        vera = ctx.new_page(); vera.set_default_timeout(8000)
        sign_in(vera, live, "vera@example.com", "pw-vera")
        vera.wait_for_selector("#role-badge:has-text('viewer')")  # invitation claimed on sign-in
        assert vera.query_selector("[data-nav=settings]") is None
        vera.click("[data-nav=jobs]")
        vera.wait_for_selector("tr[data-job]")
        assert vera.query_selector("summary:has-text('New job')") is None
        vera.click("[data-nav=pipeline]")
        vera.click("[data-app]")
        vera.wait_for_selector("#drawer")
        assert vera.query_selector("#move-btn") is None and vera.query_selector("#save-details") is None
        assert vera.query_selector("#scorecard-form") is None
        # Even with a valid viewer token, the server refuses writes.
        t = token(vera)
        aid = api(live, t, "GET", f"/v1/organizations/{oid}/applications").json()["items"][0]["id"]
        assert api(live, t, "POST", f"/v1/organizations/{oid}/applications/{aid}/transitions", json={"to_stage": "screening"}).status_code == 403
        assert api(live, t, "POST", f"/v1/organizations/{oid}/jobs", json={"title": "x", "description": "y"}).status_code == 403
        assert api(live, t, "GET", f"/v1/organizations/{oid}/members").status_code == 403
        ctx.close()

    def test_a_stranger_cannot_open_the_workspace_by_id(self, live, page, browser):
        create_workspace(page, live)
        oid = org_id(live, page)
        ctx = browser.new_context(); other = ctx.new_page(); other.set_default_timeout(8000)
        live.auth.add_user("mallory@example.com", password="pw-m")
        sign_in(other, live, "mallory@example.com", "pw-m")
        other.wait_for_selector("#create-org")
        t = token(other)
        for path in ("", "/jobs", "/applications", "/stats", "/members"):
            assert api(live, t, "GET", f"/v1/organizations/{oid}{path}").status_code == 404, path
        ctx.close()


class TestResponsiveShell:
    def test_phone_layout_uses_the_bottom_navigation(self, live, browser):
        ctx = browser.new_context(viewport={"width": 390, "height": 800})
        pg = ctx.new_page(); pg.set_default_timeout(8000)
        create_workspace(pg, live)
        assert not pg.is_visible(".sidebar")
        assert pg.is_visible(".bottomnav")
        height = pg.evaluate("document.querySelector('.bottomnav').getBoundingClientRect().height")
        assert height >= 64
        assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")  # no horizontal page scroll
        ctx.close()
