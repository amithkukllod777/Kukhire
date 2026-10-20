"""Command-line evaluation and the (org-scoped) email ingestion resolvers."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # score.py lives at the repository root

score = pytest.importorskip("score", reason="CLI dependencies (requirements.txt) are not installed")

from models import JSONResume  # noqa: E402

JOB = {
    "title": "Data Engineer", "description": "Pipelines.", "requirements": "SQL",
    "scoring_criteria": [{"key": "sql", "label": "SQL", "weight": 70}, {"key": "ops", "label": "Operations", "weight": 30}],
}


class StubProvider:
    name, model = "stub", "stub-1"

    def __init__(self):
        self.seen = ""

    def generate(self, system, user, schema):
        self.seen = system + user
        return json.dumps({"criteria": [
            {"key": "sql", "score": 8, "evidence": "Wrote warehouse models.", "missing_information": []},
            {"key": "ops", "score": 4, "evidence": "Some on-call.", "missing_information": ["incident ownership"]}],
            "summary": "Solid.", "risks": []})


@pytest.fixture()
def resume():
    return JSONResume(**{
        "basics": {"name": "Priya Private", "email": "priya@example.com"},
        "work": [{"name": "Acme", "position": "Analyst", "startDate": "2020-01-01", "summary": "Built warehouse models in SQL."}],
        "education": [{"institution": "Secret College", "area": "Statistics", "studyType": "MSc"}],
    })


def run_cli(tmp_path, monkeypatch, capsys, job=JOB, resume=None, extra=()):
    pdf = tmp_path / "r.pdf"; pdf.write_bytes(b"%PDF")
    jobfile = tmp_path / "job.json"; jobfile.write_text(json.dumps(job))
    provider = StubProvider()
    monkeypatch.setattr(score, "extract_resume", lambda path, cache: resume)
    monkeypatch.setattr(score, "default_provider", lambda name, model: provider)
    code = score.main([str(pdf), "--job", str(jobfile), *extra])
    return code, provider, capsys.readouterr()


class TestCli:
    def test_scores_against_the_jobs_own_criteria_and_writes_only_what_was_asked(self, tmp_path, monkeypatch, capsys, resume):
        out = tmp_path / "result.json"
        code, provider, io = run_cli(tmp_path, monkeypatch, capsys, resume=resume, extra=("--out", str(out)))
        assert code == 0
        result = json.loads(out.read_text())
        assert result["advisory"] is True and result["overall"] == 6.8  # (8*70 + 4*30) / 100
        assert [c["key"] for c in result["criteria"]] == ["sql", "ops"]
        assert "ADVISORY" in io.out and "not a hiring decision" in io.out
        assert sorted(p.name for p in tmp_path.iterdir()) == ["job.json", "r.pdf", "result.json"]  # no cache, no csv

    def test_the_model_never_sees_identity_or_the_school(self, tmp_path, monkeypatch, capsys, resume):
        _, provider, _ = run_cli(tmp_path, monkeypatch, capsys, resume=resume)
        for secret in ("Priya", "Private", "priya@example.com", "Secret College"):
            assert secret not in provider.seen
        assert "Data Engineer" in provider.seen and "warehouse models" in provider.seen and "Statistics" in provider.seen

    def test_refuses_without_criteria_instead_of_falling_back_to_a_default_rubric(self, tmp_path, monkeypatch, capsys, resume):
        job = {k: v for k, v in JOB.items() if k != "scoring_criteria"}
        with pytest.raises(SystemExit, match="scoring_criteria"):
            run_cli(tmp_path, monkeypatch, capsys, job=job, resume=resume)

    def test_unusable_pdf_is_an_error_not_an_empty_score(self, tmp_path, monkeypatch, capsys):
        code, provider, io = run_cli(tmp_path, monkeypatch, capsys, resume=None)
        assert code == 1 and provider.seen == "" and "Could not extract" in io.err

    def test_no_hackerrank_rubric_or_fixed_categories_remain(self):
        banned = ("position at HackerRank", "Software Intern", "open_source", "self_projects", "Hacktoberfest", "Girl Script", "GSoC")
        hits = []
        for path in list(ROOT.glob("*.py")) + list((ROOT / "app").rglob("*.py")) + list((ROOT / "prompts").rglob("*.jinja")):
            if path.name == "github.py":  # GitHub enrichment is retained, unwired; see docs/MIGRATION_AND_CUTOVER.md
                continue
            text = path.read_text(encoding="utf-8")
            hits += [f"{path.name}:{word}" for word in banned if word in text]
        assert hits == []


class TestIngestionResolversAreOrgScoped:
    def _seed(self, client, ws, job_factory):
        ws.user("a"); ws.user("b")
        org_a, org_b = ws.create_org("a", "A"), ws.create_org("b", "B")
        job_a = job_factory(org_a, ws.h("a"))
        return org_a, org_b, job_a

    def test_same_sender_in_two_workspaces_becomes_two_candidates(self, client, ws, job_factory):
        from app.repositories import SqlAlchemyCandidateResolver

        org_a, org_b, _ = self._seed(client, ws, job_factory)
        with client.app.state.session_factory() as s:
            r = SqlAlchemyCandidateResolver(s)
            a1 = r.resolve_or_create(organization_id=org_a, email="Sender@Example.com", name="S")
            a2 = r.resolve_or_create(organization_id=org_a, email="sender@example.com", name=None)
            b1 = r.resolve_or_create(organization_id=org_b, email="sender@example.com", name="S")
            s.commit()
        assert a1 == a2 and a1 != b1

    def test_a_job_from_another_workspace_is_not_attached(self, client, ws, job_factory):
        from app.repositories import SqlAlchemyApplicationResolver, SqlAlchemyCandidateResolver

        org_a, org_b, job_a = self._seed(client, ws, job_factory)
        with client.app.state.session_factory() as s:
            cand_b = SqlAlchemyCandidateResolver(s).resolve_or_create(organization_id=org_b, email="x@example.com", name="X")
            resolver = SqlAlchemyApplicationResolver(s)
            assert resolver.create_or_get(organization_id=org_b, candidate_id=cand_b, job_id=job_a["id"], source="email") is None
            cand_a = SqlAlchemyCandidateResolver(s).resolve_or_create(organization_id=org_a, email="x@example.com", name="X")
            first = resolver.create_or_get(organization_id=org_a, candidate_id=cand_a, job_id=job_a["id"], source="email")
            again = resolver.create_or_get(organization_id=org_a, candidate_id=cand_a, job_id=job_a["id"], source="email")
            s.commit()
        assert first and first == again

    def test_a_message_with_no_workspace_is_kept_for_review_not_turned_into_a_candidate(self):
        from app.email_ingestion import AttachmentInput, CandidateEmailIngestionService, CandidateEmailInput, MailProvider
        from app.ingestion_pipeline import EmailCandidatePipeline

        class Repo:
            def message_exists(self, *a): return False
            def save_raw_message(self, payload): pass

        class Boom:
            def __getattr__(self, name):
                raise AssertionError("must not be called without a workspace")

        pipeline = EmailCandidatePipeline(ingestion_service=CandidateEmailIngestionService(Repo()),
                                          candidate_resolver=Boom(), application_resolver=Boom(), resume_parser=Boom())
        result = pipeline.process(CandidateEmailInput(
            provider=MailProvider.GMAIL, mailbox_id="m", external_message_id="1", sender_email="c@example.com",
            received_at="2026-01-01T00:00:00+00:00",
            attachments=[AttachmentInput(filename="r.pdf", content_type="application/pdf", storage_key="k", size_bytes=10)]))
        assert result.ingestion.status == "needs_review" and result.ingestion.candidate_id is None
