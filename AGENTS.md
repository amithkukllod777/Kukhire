# AGENTS.md — handoff notes for the next engineer or agent

Read first: `KUKLABS_IDENTITY.md` (identity law), `docs/MVP_ARCHITECTURE.md`, `docs/HRMS_HANDOFF.md`,
`docs/MIGRATION_AND_CUTOVER.md`. Owner decision (final): KukHire is a **separate** recruitment/ATS
product; KukHRMS owns employees, onboarding, payroll, attendance and leave. Do not re-ask.

## Rules that bind every change

* **Identity:** verify tokens only through `GET {auth}/v1/auth/me` (`app/auth.py`). No local JWT
  validation, no login/user tables of our own. KukHire owns workspace membership and roles.
* **Tenancy:** every row touched is scoped by `organization_id` from the *authenticated membership*,
  never from a body or query value. Non-members get 404.
* **HRMS boundary:** never write an HRMS table; the only integration is `app/handoff.py` ->
  `POST /api/v1/hr/hires`. No salary in the payload.
* **AI is advisory:** never changes a stage; PII removed before the model; overall score computed
  locally; off by default; no paid provider without owner approval.
* **No fake status:** never report "queued"/"sent"/"completed" for something that did not happen.
* **Migrations:** add a new numbered file; never edit an applied one (checksum ledger refuses).
* **Candidate data is real people's data:** tests use synthetic data on isolated databases only; do not
  log resume text, names or emails.

## Layout

`app/api.py` factory · `app/routes/{workspace,ats}.py` · `app/{auth,tenancy,pipeline,scoring}.py` ·
`app/handoff.py`, `app/hrms_client.py` · `app/evaluation{,_core}.py` · `app/legacy_import.py` ·
`app/migrations.py` + `migrations/` · `app/static/` UI · legacy CLI parser at the repo root
(`pdf.py`, `github.py`, `score.py`, `prompts/`).

## Tests

`pytest` (SQLite). PostgreSQL: `KUKHIRE_TEST_DATABASE_URL=postgresql+psycopg://...` (a throwaway DB).
Browser journeys: `playwright install chromium`; marked `e2e`.
`tests/e2e/test_real_hrms.py` runs against a real KukBook endpoint and is skipped unless
`KUKHIRE_REAL_HRMS_URL` and `KUKHIRE_REAL_HRMS_KEY` are set.

## Open work / known gaps

1. Mailbox ingestion worker, resume upload + parsing through the API, a queue.
2. Google/phone sign-in in the UI (needs a verified token-handover design and identity-host CORS).
3. Deployment (host, PostgreSQL, backups, secrets) — none exists.
4. Cutover per `docs/MIGRATION_AND_CUTOVER.md`; the legacy KukBook page is removed only after the
   criteria in KukBook's `docs/architecture/KUKHIRE_SEPARATION.md` §6.
5. GitHub CI has never been shown to run here; a `startup_failure` is not a pass.
