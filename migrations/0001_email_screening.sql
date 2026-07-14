CREATE TABLE IF NOT EXISTS mailbox_connections (
    id VARCHAR(36) PRIMARY KEY,
    organization_id VARCHAR(36) NOT NULL REFERENCES organizations(id),
    provider VARCHAR(32) NOT NULL,
    provider_account_id VARCHAR(255) NOT NULL,
    email_address VARCHAR(320) NOT NULL,
    encrypted_access_token TEXT NOT NULL,
    encrypted_refresh_token TEXT,
    token_expires_at TIMESTAMP,
    watch_cursor VARCHAR(500),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_mailbox_connections_organization_id
    ON mailbox_connections (organization_id);

CREATE TABLE IF NOT EXISTS ingested_emails (
    id VARCHAR(36) PRIMARY KEY,
    mailbox_id VARCHAR(160) NOT NULL,
    provider VARCHAR(32) NOT NULL,
    external_message_id VARCHAR(255) NOT NULL,
    thread_id VARCHAR(255),
    sender_name VARCHAR(180),
    sender_email VARCHAR(320) NOT NULL,
    subject VARCHAR(500) NOT NULL DEFAULT '',
    body_text TEXT NOT NULL DEFAULT '',
    received_at VARCHAR(64) NOT NULL,
    job_id VARCHAR(36) REFERENCES jobs(id),
    attachments JSON NOT NULL,
    status VARCHAR(40) NOT NULL DEFAULT 'received',
    error_message TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_ingested_email_message UNIQUE (mailbox_id, external_message_id)
);

CREATE INDEX IF NOT EXISTS ix_ingested_emails_mailbox_id ON ingested_emails (mailbox_id);
CREATE INDEX IF NOT EXISTS ix_ingested_emails_sender_email ON ingested_emails (sender_email);
CREATE INDEX IF NOT EXISTS ix_ingested_emails_job_id ON ingested_emails (job_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_candidate_email_normalized
    ON candidates (LOWER(email)) WHERE email IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_application_candidate_job
    ON applications (candidate_id, job_id);
