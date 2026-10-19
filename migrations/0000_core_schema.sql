-- Core tables the application models (app/db_models.py on main before this migration) and
-- migration 0001 both assume exist. 0001 references organizations/jobs/candidates/applications
-- but nothing ever created them, so 0001 could not run on a fresh database.
--
-- IF NOT EXISTS throughout: a database that already has these tables (created by hand or by
-- SQLAlchemy create_all) is left untouched. Tenant scoping and the richer ATS columns are added by
-- 0002; this file deliberately reproduces the ORIGINAL shape so that upgrade has a defined start.

CREATE TABLE IF NOT EXISTS organizations (
    id VARCHAR(36) PRIMARY KEY,
    name VARCHAR(160) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS jobs (
    id VARCHAR(36) PRIMARY KEY,
    organization_id VARCHAR(36) NOT NULL REFERENCES organizations(id),
    title VARCHAR(180) NOT NULL,
    description TEXT NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'draft',
    scoring_weights JSON NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_jobs_organization_id ON jobs (organization_id);

CREATE TABLE IF NOT EXISTS candidates (
    id VARCHAR(36) PRIMARY KEY,
    full_name VARCHAR(180) NOT NULL,
    email VARCHAR(320),
    phone VARCHAR(40),
    resume_storage_key VARCHAR(500),
    parsed_resume JSON NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_candidates_email ON candidates (email);

CREATE TABLE IF NOT EXISTS applications (
    id VARCHAR(36) PRIMARY KEY,
    job_id VARCHAR(36) NOT NULL REFERENCES jobs(id),
    candidate_id VARCHAR(36) NOT NULL REFERENCES candidates(id),
    stage VARCHAR(40) NOT NULL DEFAULT 'applied',
    score FLOAT,
    evaluation JSON NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_applications_job_id ON applications (job_id);
CREATE INDEX IF NOT EXISTS ix_applications_candidate_id ON applications (candidate_id);
