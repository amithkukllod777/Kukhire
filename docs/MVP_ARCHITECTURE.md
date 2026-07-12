# KukHire MVP Architecture

## Product boundary

KukHire is an AI-assisted applicant tracking and candidate evaluation product. AI output is advisory and must never make the final hiring decision.

## MVP modules

1. Jobs and scoring criteria
2. Candidates and resume ingestion
3. Resume parsing and structured profiles
4. Job-specific explainable scoring
5. Hiring pipeline and stage history
6. Interview scheduling and scorecards
7. Recruiter dashboard and funnel analytics
8. Organization, users, roles, and audit trail

## Proposed system

- Web: React/TypeScript, Tailwind CSS, shadcn/ui
- API: FastAPI
- Database: PostgreSQL
- Object storage: S3-compatible storage for resumes
- Queue: Redis plus Celery/RQ for parsing and scoring jobs
- AI providers: local Ollama plus hosted provider adapters
- Existing engine: retain PDF extraction, prompt templates, GitHub enrichment, and provider abstractions after refactoring behind services

## Initial API boundary

- `GET /health`
- `GET /v1/capabilities`
- `POST /v1/evaluations`

The evaluation endpoint currently returns a queued contract. Next implementation connects persistence, background workers, job criteria, and the existing resume-scoring engine.

## Core scoring rule

Scores must be job-specific, evidence-backed, configurable by recruiters, and accompanied by missing-information and risk indicators. Protected attributes and irrelevant personal details must not be used in scoring.

## Delivery sequence

1. API foundation and database schema
2. Resume upload and parsing worker
3. Jobs, candidates, and pipeline APIs
4. Job-specific scoring service
5. Recruiter dashboard UI
6. Interview workflow
7. Auth, organization tenancy, audit logs, testing, and deployment
