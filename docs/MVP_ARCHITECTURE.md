# KukHire architecture — what is built, and what is not

## Product boundary

KukHire is a **separate** recruitment / applicant-tracking product. KukHRMS (in the KukBook
repository) owns employee records, onboarding, payroll, attendance and leave; KukHire owns jobs,
candidates, applications, the pipeline, scorecards and referral *attribution*. The two meet at one
authenticated HTTP call ([HRMS_HANDOFF.md](HRMS_HANDOFF.md)). KukMeet (video interviews) and
KukCalendar (scheduling) are separate products; KukHire may link to them but does not absorb them.

AI output is advisory and never makes or changes a hiring decision.

## Built (each item has automated tests)

| Area | Notes |
|---|---|
| Identity | Bearer token verified **only** by `GET {KUKHIRE_AUTH_BASE_URL}/v1/auth/me` with `X-Kuklabs-Product: kukhire`. No local JWT validation. Unset base URL => authenticated routes answer 503 (fail closed). |
| Workspaces | KukHire owns membership and roles: `admin`, `recruiter`, `interviewer`, `viewer` (capability matrix in `app/tenancy.py`). Non-members get **404**, not 403. Invitations are claimed only by a verified, matching email. |
| Jobs | CRUD, with weighted scoring criteria validated (`app/scoring.py`). |
| Candidates / applications | A candidate is a person (unique by normalized email **per workspace**); an application is that person for one job. |
| Pipeline | `applied -> screening -> interview -> offer -> hired`; `rejected` and `withdrawn` are exits from any open stage (reopening needs a reason); `hired` is final (reversal is an HRMS decision). Invalid transitions are refused; every move is recorded in `application_stage_history`. Nothing automated (AI, import, email) can move an application. |
| Scorecards | Linked to applications, 1-5 in one-decimal steps. |
| Handoff | `hired` application -> HRMS employee reference ([HRMS_HANDOFF.md](HRMS_HANDOFF.md)). |
| Evaluation | Job-specific, evidence-backed, **advisory**, **off by default**, local Ollama only. PII (name, contact, links) is stripped before the model sees anything; the overall score is computed by us, not taken from the model. |
| Audit | Security-relevant actions are written to `audit_log`. |
| UI | Plain-JS recruiter UI (`app/static`), exercised by 11 headless-browser journeys. |
| Legacy import | See [MIGRATION_AND_CUTOVER.md](MIGRATION_AND_CUTOVER.md). |

## Not built (reported `unavailable` by `GET /v1/capabilities`)

* Mailbox ingestion worker (contracts and tables exist; no connector is wired).
* Resume upload and parsing through the API (the parser runs from `score.py`).
* Google / phone sign-in inside the KukHire UI. Email + password only. Whether the identity host
  allows the KukHire origin (CORS) is **unverified**.
* Background queue / workers, interview scheduling, funnel analytics beyond the basic `/stats` counts.
* A production deployment, domain, or backups. None exists; nothing here claims one.

## Stack

FastAPI + SQLAlchemy 2; PostgreSQL in production (SQLite for local development and tests only);
ordered migrations with a checksum ledger (`app/migrations.py`, `migrations/`). The test suite runs
on SQLite and PostgreSQL 16.

## Evaluation rule

Scores must be job-specific (the job's own criteria; there is no built-in rubric), evidence-backed,
and accompanied by missing-information notes. Protected attributes and irrelevant personal details
must not be used. Output is a suggestion a recruiter may ignore.
