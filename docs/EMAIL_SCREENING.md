# KukHire Email Screening

## Goal

Convert candidate application emails and resume attachments into structured candidate and application records without requiring recruiters to upload every resume manually.

## Supported sources

1. Gmail through OAuth and Gmail API.
2. Microsoft 365 through OAuth and Microsoft Graph.
3. Generic mailboxes through IMAP for legacy accounts.
4. Job-specific forwarding addresses such as `job-<id>@apply.kukhire.com`.

## Recommended flow

```text
Mailbox connector
  -> webhook or scheduled sync
  -> raw message store
  -> attachment malware scan
  -> message deduplication
  -> resume attachment selection
  -> resume parser
  -> candidate identity resolution
  -> application creation
  -> job-specific AI evaluation
  -> recruiter review queue
```

## Data extracted

- Sender name and email
- Phone and location from resume or email body
- Resume file and parsed profile
- Source mailbox and original message ID
- Applied job, where determinable
- Email subject and body
- Cover letter or supporting note
- Skills, work history, education, projects and links
- Screening score with evidence and warnings

## Job matching rules

Use deterministic identifiers before AI inference:

1. A job-specific inbound email address.
2. A KukHire job code in the subject or body.
3. A reply to a tracked outbound job thread.
4. Recruiter-defined mailbox rules.
5. AI suggestion only when deterministic matching fails.

AI must not silently attach a candidate to a job when confidence is low. Such messages go to an unassigned applications queue.

## Identity and duplicate handling

Candidate identity resolution should use normalized email, verified phone, and optional profile URLs. Name alone is not a safe deduplication key.

Every provider message is idempotent on:

```text
organization_id + mailbox_id + external_message_id
```

Resume hashes should also be stored to detect repeated applications and agency submissions.

## Security requirements

- Store OAuth refresh tokens encrypted through a secrets manager.
- Request the minimum mailbox scopes required.
- Never log email bodies, access tokens or resume contents.
- Scan attachments before parsing.
- Enforce file type and size limits.
- Keep raw email retention configurable per organization.
- Maintain consent, deletion and audit records.
- Do not auto-reject candidates solely from AI output.

## Recruiter experience

The recruiter dashboard needs:

- Connected mailbox status
- Sync history and failures
- New candidates from email
- Unassigned applications
- Duplicate warnings
- Missing resume queue
- Suggested job match
- Parsed candidate preview
- Approve, edit, merge or reject actions

## MVP sequence

1. Job-specific forwarding email ingestion.
2. Gmail OAuth connector.
3. Resume parsing and candidate creation.
4. Duplicate detection.
5. Job-specific screening and recruiter review.
6. Microsoft 365 and generic IMAP connectors.
7. Reply classification and automated communication workflows.
