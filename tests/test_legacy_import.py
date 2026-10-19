"""Legacy import: dry-run parity, idempotency, honest quarantine, organization mapping, employee links.

All data here is SYNTHETIC. The bundle shape is the one scripts/export-legacy-recruitment.cjs in
kukbook-erp writes (raw column names).
"""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess

import httpx
import pytest
from sqlalchemy import func, select

from app import legacy_import as li
from app.db_models import (
    Application, ApplicationStageHistory, Candidate, HiringHandoff, Job, LegacyImportMap,
    LegacyImportQuarantine, Organization, OrganizationMember, Scorecard,
)
from app.hrms_client import HrmsClient


def job(i, **o):
    return {"jp_id": i, "jp_companyId": 7, "jp_title": f"Job {i}", "jp_department": "Eng", "jp_location": "Pune", "jp_type": "full_time",
            "jp_description": "d", "jp_requirements": "r", "jp_salaryFrom": "50000.00", "jp_salaryTo": "90000.00", "jp_status": "open",
            "jp_openings": 2, "jp_postedDate": "2026-01-02", "jp_closingDate": "2026-12-31", "jp_createdAt": "2026-01-02 10:00:00", **o}


def cand(i, job_id=1, **o):
    return {"cand_id": i, "cand_companyId": 7, "cand_jobId": job_id, "cand_name": f"Person {i}", "cand_email": f"p{i}@example.com", "cand_phone": "9",
            "cand_stage": "new", "cand_source": "manual", "cand_resumeUrl": None, "cand_notes": "n", "cand_rating": 4, "cand_expectedSalary": "75000.00",
            "cand_noticePeriod": "30 days", "cand_interviewDate": "2026-02-03", "cand_assignedTo": "Hana", "cand_createdAt": "2026-01-05 09:00:00",
            "cand_updatedAt": "2026-01-06 09:00:00", "cand_employeeId": None, "cand_referrerEmployeeId": None, "cand_referralBonusStatus": None, **o}


def card(i, cand_id, **o):
    return {"his_id": i, "his_companyId": 7, "his_candidateId": cand_id, "his_interviewerName": "Ivy", "his_overall": "4.5", "his_technical": "4.0",
            "his_culture": None, "his_notes": "good", "his_interviewDate": "2026-02-04", "his_createdBy": 1, "his_createdAt": "2026-02-04 12:00:00", **o}


def make_bundle(jobs=None, cands=None, cards=None, employees=None, company="7"):
    tables = {"job_positions": jobs or [], "candidates": cands or [], "hr_interview_scorecards": cards or [],
              "employees": employees if employees is not None else [{"id": 77, "empId": "EMP-077"}]}
    checksum = hashlib.sha256(json.dumps(tables, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    return {"format": "kukbook-recruitment-export", "format_version": 1, "source_system": "kukbook-erp", "exported_at": "2026-10-01T00:00:00Z",
            "company": {"ref": company, "name": "Legacy Co"}, "tables": tables, "counts": {k: len(v) for k, v in tables.items()}, "checksum": checksum}


def rich_bundle():
    return make_bundle(
        jobs=[job(1), job(2, jp_status="closed")],
        cands=[
            cand(1, 1, cand_stage="new"),
            cand(2, 1, cand_stage="hired", cand_employeeId=77, cand_source="referral", cand_referrerEmployeeId=12, cand_referralBonusStatus="pending",
                 cand_resumeUrl="https://files.example.com/cv2.pdf"),
            cand(3, 2, cand_stage="interview", cand_email="p1@example.com"),  # same person as #1, different job
            cand(4, 2, cand_stage="rejected", cand_email=None, cand_name="No Email"),
            cand(5, 1, cand_stage="offer"),
        ],
        cards=[card(1, 2), card(2, 2, his_interviewerName="Jo", his_overall="3.7"), card(3, 5)],
    )


@pytest.fixture()
def org(client, ws):
    ws.user("owner")
    return ws.create_org("owner", "Imported Co")


def session(client):
    return client.app.state.session_factory()


def counts(s, org_id):
    return {m.__tablename__: s.scalar(select(func.count()).select_from(m).where(m.organization_id == org_id))
            for m in (Job, Candidate, Application, Scorecard, HiringHandoff, ApplicationStageHistory, LegacyImportMap, LegacyImportQuarantine)}


def do_import(client, bundle, org_id, *, apply=True, link=True):
    with session(client) as s:
        organization = li.resolve_organization(s, bundle, organization_id=org_id, link_company=link)
        report = li.run_import(s, bundle, organization, dry_run=not apply)
        s.commit() if apply else s.rollback()
    return report


class TestHappyPath:
    def test_everything_maps_and_reconciles(self, client, org):
        report = do_import(client, rich_bundle(), org)
        assert report.balanced
        assert (report.tables["job_positions"].imported, report.tables["candidates"].imported, report.tables["hr_interview_scorecards"].imported) == (2, 5, 3)
        with session(client) as s:
            c = counts(s, org)
            # 5 legacy rows, but #1 and #3 are the same person (same email) => 4 people, 5 applications.
            assert (c["jobs"], c["candidates"], c["applications"], c["scorecards"]) == (2, 4, 5, 3)
            assert c["legacy_import_quarantine"] == 0 and c["application_stage_history"] == 5
            stages = sorted(a.stage for a in s.scalars(select(Application).where(Application.organization_id == org)))
            assert stages == ["applied", "hired", "interview", "offer", "rejected"]  # legacy new/hired/interview/rejected/offer, each kept
            by_src = {m.source_id: m for m in s.scalars(select(LegacyImportMap).where(LegacyImportMap.source_table == "candidates"))}
            assert s.get(Application, by_src["1"].target_id).stage == "applied"       # new -> applied
            assert s.get(Application, by_src["3"].target_id).stage == "interview"
            assert s.get(Application, by_src["1"].target_id).candidate_id == s.get(Application, by_src["3"].target_id).candidate_id

    def test_fields_and_history_are_preserved(self, client, org):
        do_import(client, rich_bundle(), org)
        with session(client) as s:
            m = s.scalar(select(LegacyImportMap).where(LegacyImportMap.source_table == "candidates", LegacyImportMap.source_id == "2"))
            app = s.get(Application, m.target_id)
            assert (app.source, app.referrer_employee_ref, float(app.expected_salary), app.notice_period, app.rating, app.assigned_to) == ("referral", "12", 75000.0, "30 days", 4, "Hana")
            assert app.created_at.isoformat() == "2026-01-05T09:00:00" and app.interview_date.isoformat() == "2026-02-03"
            assert s.get(Candidate, app.candidate_id).resume_url == "https://files.example.com/cv2.pdf"
            # Fields with no home in KukHire survive verbatim in the mapping snapshot — the bonus status is
            # NOT a KukHire fact (approving/paying it is HRMS/payroll), but it is not lost either.
            assert m.source_snapshot["cand_referralBonusStatus"] == "pending"
            j = s.scalar(select(LegacyImportMap).where(LegacyImportMap.source_table == "job_positions", LegacyImportMap.source_id == "1"))
            assert j.source_snapshot["jp_salaryFrom"] == "50000.00"
            assert s.get(Job, j.target_id).closing_date.isoformat() == "2026-12-31"

    def test_scorecards_attach_to_the_right_application_with_exact_scores(self, client, org):
        do_import(client, rich_bundle(), org)
        with session(client) as s:
            m = s.scalar(select(LegacyImportMap).where(LegacyImportMap.source_table == "candidates", LegacyImportMap.source_id == "2"))
            cards = s.scalars(select(Scorecard).where(Scorecard.application_id == m.target_id).order_by(Scorecard.created_at)).all()
            assert sorted(float(c.overall) for c in cards) == [3.7, 4.5]  # one decimal kept, nothing rounded
            assert all(c.interviewer_user_id is None for c in cards)  # legacy had names only; no user is invented

    def test_employee_links_become_completed_handoffs(self, client, org):
        report = do_import(client, rich_bundle(), org)
        assert report.employee_links == {"source_rows_with_employee_link": 1, "links_present_in_kukhire": 1}
        with session(client) as s:
            h = s.scalar(select(HiringHandoff).where(HiringHandoff.organization_id == org))
            assert (h.status, h.employee_ref, h.employee_code, h.external_ref) == ("completed", "77", "EMP-077", "legacy:7:2")


class TestDryRunAndIdempotency:
    def test_dry_run_commits_nothing_but_reports_exactly_what_apply_would_do(self, client, org):
        bundle = rich_bundle()
        dry = do_import(client, bundle, org, apply=False)
        with session(client) as s:
            assert all(v == 0 for v in counts(s, org).values())
            assert s.get(Organization, org).external_company_ref is None  # not even the link was kept
        real = do_import(client, bundle, org, apply=True)
        assert {k: (t.imported, t.quarantined) for k, t in dry.tables.items()} == {k: (t.imported, t.quarantined) for k, t in real.tables.items()}

    def test_second_run_imports_nothing_and_changes_nothing(self, client, org):
        bundle = rich_bundle()
        do_import(client, bundle, org)
        with session(client) as s:
            before = counts(s, org)
        again = do_import(client, bundle, org)
        assert again.balanced
        assert (again.tables["job_positions"].already_imported, again.tables["candidates"].already_imported, again.tables["hr_interview_scorecards"].already_imported) == (2, 5, 3)
        assert all(t.imported == 0 for t in again.tables.values())
        with session(client) as s:
            assert counts(s, org) == before
        assert again.employee_links["links_present_in_kukhire"] == 1


class TestHonestQuarantine:
    def bundle(self):
        return make_bundle(
            jobs=[job(1), job(2, jp_status="paused-by-ceo"), job(3, jp_title="  ")],
            cands=[
                cand(1, 1),                                              # fine
                cand(2, 1, cand_stage="shortlisted", cand_email="s@example.com"),   # unknown stage
                cand(3, 99, cand_email="t@example.com"),                 # job does not exist
                cand(4, 2, cand_email="u@example.com"),                  # its job was quarantined
                cand(5, 1, cand_name="   ", cand_email="v@example.com"), # empty name
                cand(6, 1, cand_email="p1@example.com"),                 # same person + same job as #1
                cand(7, 1, cand_email="w@example.com", cand_resumeUrl="C:\\Users\\x\\cv.pdf"),   # not a usable link
                cand(8, 1, cand_email="not-an-email"),                   # invalid email => kept, without the email
            ],
            cards=[card(1, 1), card(2, 3), card(3, 404), card(4, 1, his_overall="9.5"), card(5, 1, his_technical="0")],
        )

    def test_unmappable_rows_are_quarantined_with_reasons_not_guessed(self, client, org):
        report = do_import(client, self.bundle(), org)
        by_key = {(q["table"], q["source_id"]): q["reason"] for q in report.quarantine}
        assert "unknown stage" in by_key[("candidates", "2")]
        assert "job was not imported" in by_key[("candidates", "3")] and "job was not imported" in by_key[("candidates", "4")]
        assert "empty name" in by_key[("candidates", "5")]
        assert "duplicate application" in by_key[("candidates", "6")]
        assert "unknown job status" in by_key[("job_positions", "2")] and "empty title" in by_key[("job_positions", "3")]
        assert "no imported candidate" in by_key[("hr_interview_scorecards", "3")]   # candidate 404 never existed
        assert "no imported candidate" in by_key[("hr_interview_scorecards", "2")]   # candidate 3 was quarantined
        assert "outside the 1-5 scale" in by_key[("hr_interview_scorecards", "4")] and "outside the 1-5 scale" in by_key[("hr_interview_scorecards", "5")]
        assert report.balanced, "every source row is accounted for: imported, already imported, or quarantined"

    def test_quarantined_rows_keep_their_original_data(self, client, org):
        do_import(client, self.bundle(), org)
        with session(client) as s:
            q = s.scalar(select(LegacyImportQuarantine).where(LegacyImportQuarantine.source_table == "candidates", LegacyImportQuarantine.source_id == "2"))
            assert q.payload["cand_stage"] == "shortlisted" and q.payload["cand_email"] == "s@example.com" and q.resolved_at is None

    def test_good_rows_around_bad_ones_still_import(self, client, org):
        report = do_import(client, self.bundle(), org)
        assert report.tables["job_positions"].imported == 1
        assert report.tables["candidates"].imported == 3  # #1, #7, #8
        assert report.tables["hr_interview_scorecards"].imported == 1  # card 1 only
        with session(client) as s:
            assert s.scalar(select(func.count()).select_from(Application).where(Application.organization_id == org)) == 3

    def test_unusable_resume_reference_is_kept_for_review_not_imported_as_a_link(self, client, org):
        report = do_import(client, self.bundle(), org)
        assert report.tables["candidates"].warnings["resume_reference_quarantined"] == 1
        with session(client) as s:
            m = s.scalar(select(LegacyImportMap).where(LegacyImportMap.source_table == "candidates", LegacyImportMap.source_id == "7"))
            assert s.get(Candidate, s.get(Application, m.target_id).candidate_id).resume_url is None
            q = s.scalar(select(LegacyImportQuarantine).where(LegacyImportQuarantine.source_table == "candidates.resume_url"))
            assert q.payload["cand_resumeUrl"] == "C:\\Users\\x\\cv.pdf"

    def test_invalid_email_is_dropped_with_a_warning_but_the_original_survives_in_the_snapshot(self, client, org):
        report = do_import(client, self.bundle(), org)
        assert report.tables["candidates"].warnings["invalid_email_not_imported"] == 1
        with session(client) as s:
            m = s.scalar(select(LegacyImportMap).where(LegacyImportMap.source_table == "candidates", LegacyImportMap.source_id == "8"))
            assert s.get(Candidate, s.get(Application, m.target_id).candidate_id).email is None
            assert m.source_snapshot["cand_email"] == "not-an-email"

    def test_a_quarantined_row_is_resolved_once_its_data_is_fixed_and_it_is_imported_again(self, client, org):
        bundle = self.bundle()
        do_import(client, bundle, org)
        fixed = copy.deepcopy(bundle)
        fixed["tables"]["candidates"][1]["cand_stage"] = "screening"
        fixed["checksum"] = hashlib.sha256(json.dumps(fixed["tables"], separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        report = do_import(client, fixed, org)
        assert report.tables["candidates"].imported == 1
        with session(client) as s:
            q = s.scalar(select(LegacyImportQuarantine).where(LegacyImportQuarantine.source_table == "candidates", LegacyImportQuarantine.source_id == "2"))
            assert q.resolved_at is not None


class TestOrganizationMapping:
    def test_wrong_company_is_refused(self, client, org):
        with session(client) as s:
            s.get(Organization, org).external_company_ref = "999"
            s.commit()
        with pytest.raises(li.ImportError_, match="linked to company 999"):
            do_import(client, rich_bundle(), org)

    def test_linking_an_unlinked_workspace_must_be_explicit(self, client, org):
        with pytest.raises(li.ImportError_, match="--link-company"):
            do_import(client, rich_bundle(), org, link=False)
        do_import(client, rich_bundle(), org, link=True)
        with session(client) as s:
            assert s.get(Organization, org).external_company_ref == "7"

    def test_one_company_cannot_be_claimed_by_two_workspaces(self, client, ws, org):
        do_import(client, rich_bundle(), org)
        ws.user("second")
        other = ws.create_org("second", "Other")
        with pytest.raises(li.ImportError_, match="already linked to a different workspace"):
            do_import(client, rich_bundle(), other)

    def test_import_never_touches_another_workspace(self, client, ws, org, job_factory, application_factory):
        ws.user("bystander")
        other = ws.create_org("bystander", "Bystander")
        job_b = job_factory(other, ws.h("bystander"))
        application_factory(other, ws.h("bystander"), job_b["id"], email="p1@example.com")  # same email as an imported person
        with session(client) as s:
            before = counts(s, other)
        do_import(client, rich_bundle(), org)
        with session(client) as s:
            assert counts(s, other) == before
            assert s.scalar(select(func.count()).select_from(Candidate).where(Candidate.organization_id == other)) == 1

    def test_create_organization_invites_an_admin_instead_of_inventing_a_user(self, client):
        with session(client) as s:
            created = li.resolve_organization(s, rich_bundle(), create_name="Fresh Co", admin_email="Boss@Example.com")
            s.commit()
            m = s.scalar(select(OrganizationMember).where(OrganizationMember.organization_id == created.id))
            assert (m.email, m.role, m.status, m.user_id) == ("boss@example.com", "admin", "invited", None)
            assert created.external_company_ref == "7"


class TestBundleIntegrity:
    def write(self, tmp_path, bundle):
        p = tmp_path / "bundle.json"
        p.write_text(json.dumps(bundle))
        return p

    def test_valid_bundle_loads(self, tmp_path):
        assert li.load_bundle(self.write(tmp_path, rich_bundle()))["company"]["ref"] == "7"

    def test_tampered_or_truncated_bundles_are_refused(self, tmp_path):
        b = rich_bundle()
        b["tables"]["candidates"][0]["cand_name"] = "Edited In Transit"
        with pytest.raises(li.ImportError_, match="checksum"):
            li.load_bundle(self.write(tmp_path, b))

    @pytest.mark.parametrize("mutate,match", [
        (lambda b: b.update(format="other"), "not a kukbook"), (lambda b: b.update(format_version=2), "not a kukbook"),
        (lambda b: b.update(source_system="x"), "source_system"), (lambda b: b["company"].update(ref=""), "company.ref"),
    ])
    def test_wrong_shape_is_refused(self, tmp_path, mutate, match):
        b = rich_bundle(); mutate(b)
        with pytest.raises(li.ImportError_, match=match):
            li.load_bundle(self.write(tmp_path, b))

    @pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
    def test_checksum_algorithm_matches_the_node_exporter(self, tmp_path):
        """The exporter hashes JSON.stringify(tables). Prove Python agrees on awkward content."""
        b = rich_bundle()
        b["tables"]["candidates"][0]["cand_name"] = "Zoë \"Quote\" \\ Müller 日本語 \n tab\t"
        b["tables"]["candidates"][0]["cand_rating"] = 3
        script = "const c=require('crypto');const t=JSON.parse(require('fs').readFileSync(process.argv[1],'utf8')).tables;console.log(c.createHash('sha256').update(JSON.stringify(t)).digest('hex'))"
        p = tmp_path / "t.json"; p.write_text(json.dumps(b), encoding="utf-8")
        node = subprocess.run(["node", "-e", script, str(p)], capture_output=True, text=True, check=True).stdout.strip()
        assert node == hashlib.sha256(json.dumps(b["tables"], separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


class TestCli:
    def run(self, tmp_path, database_url, bundle, *extra):
        p = tmp_path / "b.json"; p.write_text(json.dumps(bundle))
        return li.main(["--bundle", str(p), "--database-url", database_url, *extra])

    def test_default_is_a_dry_run(self, tmp_path, database_url, engine, capsys):
        assert self.run(tmp_path, database_url, rich_bundle(), "--create-organization", "Cli Co") == 0
        out = capsys.readouterr().out
        assert "DRY RUN (nothing committed)" in out and "RECONCILED" in out
        from sqlalchemy.orm import Session
        with Session(engine) as s:
            assert s.scalar(select(func.count()).select_from(Organization)) == 0

    def test_apply_commits_and_a_rerun_is_a_no_op(self, tmp_path, database_url, engine, capsys):
        assert self.run(tmp_path, database_url, rich_bundle(), "--create-organization", "Cli Co", "--admin-email", "boss@example.com", "--apply") == 0
        from sqlalchemy.orm import Session
        with Session(engine) as s:
            org = s.scalar(select(Organization))
            assert s.scalar(select(func.count()).select_from(Application)) == 5
        capsys.readouterr()
        report = tmp_path / "r.json"
        assert self.run(tmp_path, database_url, rich_bundle(), "--organization-id", org.id, "--apply", "--report", str(report)) == 0
        data = json.loads(report.read_text())
        assert data["balanced"] and all(t["imported"] == 0 for t in data["tables"].values())

    def test_refuses_without_a_target_workspace(self, tmp_path, database_url, engine, capsys):
        assert self.run(tmp_path, database_url, rich_bundle()) == 1
        assert "Name the target workspace" in capsys.readouterr().err


class TestImportedDataBehavesInTheProduct:
    @pytest.fixture()
    def imported(self, client, ws):
        ws.user("boss", email="boss@example.com")
        with session(client) as s:
            created = li.resolve_organization(s, rich_bundle(), create_name="Imported Co", admin_email="boss@example.com")
            li.run_import(s, rich_bundle(), created, dry_run=False)
            s.commit()
            org_id = created.id
        h = ws.h("boss")
        assert client.get("/v1/me", headers=h).json()["organizations"][0]["role"] == "admin"  # invitation claimed
        return org_id, h

    def test_pipeline_and_history_show_imported_work(self, client, imported):
        org_id, h = imported
        stats = client.get(f"/v1/organizations/{org_id}/stats", headers=h).json()
        assert stats["by_stage"] == {"applied": 1, "screening": 0, "interview": 1, "offer": 1, "hired": 1, "rejected": 1, "withdrawn": 0}
        apps = {a["candidate_name"]: a for a in client.get(f"{'/v1/organizations/' + org_id}/applications", headers=h).json()["items"]}
        detail = client.get(f"/v1/organizations/{org_id}/applications/{apps['Person 2']['id']}", headers=h).json()
        assert detail["history"][0]["to_stage"] == "hired" and "imported from KukBook" in detail["history"][0]["reason"]
        assert detail["handoff"]["employee_ref"] == "77" and detail["handoff"]["status"] == "completed"

    def test_an_imported_hire_with_an_employee_link_never_calls_hrms_again(self, client, imported):
        org_id, h = imported
        app = next(a for a in client.get(f"/v1/organizations/{org_id}/applications", headers=h).json()["items"] if a["candidate_name"] == "Person 2")
        r = client.post(f"/v1/organizations/{org_id}/applications/{app['id']}/handoff", json={}, headers=h)
        assert r.status_code == 200 and r.json()["employee_ref"] == "77"  # returned from the preserved link; no HRMS configured, none needed

    def test_a_hired_import_without_a_link_hands_off_and_tells_hrms_which_legacy_candidate_it_is(self, client, ws, make_app, settings):
        from fastapi.testclient import TestClient

        sent = []

        def hrms(request):
            sent.append(json.loads(request.content))
            return httpx.Response(201, json={"data": {"handoffId": 1, "employeeId": 9, "empId": "EMP-9", "created": True}})

        bundle = make_bundle(jobs=[job(1)], cands=[cand(30, 1, cand_stage="hired")])
        ws.user("boss", email="boss@example.com")
        c = TestClient(make_app(hrms_client_factory=lambda u, k: HrmsClient(u, k, transport=httpx.MockTransport(hrms))))
        ws.client = c
        with c.app.state.session_factory() as s:
            created = li.resolve_organization(s, bundle, create_name="Co", admin_email="boss@example.com")
            li.run_import(s, bundle, created, dry_run=False); s.commit(); org_id = created.id
        h = ws.h("boss")
        c.get("/v1/me", headers=h)
        assert c.put(f"/v1/organizations/{org_id}/integrations/hrms", json={"hrms_base_url": "https://hrms.example.com", "api_key": "kb_live_0123456789ab"}, headers=h).status_code == 200
        app = c.get(f"/v1/organizations/{org_id}/applications", headers=h).json()["items"][0]
        assert c.post(f"/v1/organizations/{org_id}/applications/{app['id']}/handoff", json={}, headers=h).status_code == 200
        assert sent[0]["legacyCandidateId"] == 30  # lets HRMS adopt an existing employee link instead of duplicating

    def test_erasing_an_imported_person_removes_their_preserved_source_rows_too(self, client, imported):
        org_id, h = imported
        app = next(a for a in client.get(f"/v1/organizations/{org_id}/applications", headers=h).json()["items"] if a["candidate_name"] == "Person 5")
        assert client.delete(f"/v1/organizations/{org_id}/candidates/{app['candidate_id']}", headers=h).status_code == 204
        with session(client) as s:
            snaps = [m.source_snapshot for m in s.scalars(select(LegacyImportMap).where(LegacyImportMap.organization_id == org_id))]
            assert not any(sn and sn.get("cand_name") == "Person 5" for sn in snaps)
            assert not any(sn and sn.get("his_candidateId") == 5 for sn in snaps)


class TestRealExporterOutput:
    """tests/fixtures/kukbook_export_sample.json was produced by the REAL exporter
    (kukbook-erp scripts/export-legacy-recruitment.cjs) from synthetic data in a MySQL-protocol database.
    It pins the column names, the value formats (decimals as strings, 'YYYY-MM-DD HH:MM:SS' timestamps)
    and the checksum the importer must accept."""

    FIXTURE = "tests/fixtures/kukbook_export_sample.json"

    def test_importer_accepts_and_reconciles_the_real_bundle(self, client, org):
        bundle = li.load_bundle(self.FIXTURE)  # checksum computed by node over unicode content
        report = do_import(client, bundle, org)
        assert report.balanced
        assert {k: (t.source, t.imported, t.quarantined) for k, t in report.tables.items()} == {
            "job_positions": (2, 2, 0), "candidates": (4, 3, 1), "hr_interview_scorecards": (3, 2, 1)}
        reasons = {(q["table"], q["reason"].split(";")[0]) for q in report.quarantine}
        assert ("candidates", "unknown stage 'on_hold_by_boss'") in reasons
        with session(client) as s:
            names = {c.full_name for c in s.scalars(select(Candidate).where(Candidate.organization_id == org))}
            assert "Zoë Müller 日本語" in names  # unicode survived export -> JSON -> import
            h = s.scalar(select(HiringHandoff).where(HiringHandoff.organization_id == org))
            assert h.status == "completed" and h.employee_code == "EMP-940"
