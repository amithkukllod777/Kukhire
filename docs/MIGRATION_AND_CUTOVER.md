# Schema migrations, legacy import, cutover and rollback

Nothing here has been run against production. Production migration is a separate, owner-approved act.

## 1. KukHire schema

`python -m app.migrations [--dry-run]` applies `migrations/` in order and records each in
`schema_migrations` with a checksum (editing an applied migration is refused; add a new one).

| Migration | What |
|---|---|
| `0000_core_schema.sql` | Fresh database: organizations, members, jobs, candidates, applications, history, scorecards, handoffs, integrations, evaluations, audit, import tables. |
| `0001_email_screening.sql` | Ingestion tables (kept byte-identical to what pre-existing databases already ran). |
| `0002_tenant_scoped_ats.py` | **Upgrade path** for databases created before tenancy: adds organization columns, splits candidates that were shared across organizations, keeps unattributable rows (never deletes them), replaces the global unique email with a per-organization one. |

Fresh DB: all three run. Upgrade: only the missing ones. Both tested on SQLite and PostgreSQL 16.
KukHire never drops a table.

## 2. Moving legacy KukBook recruitment data

```text
KukBook box                                   KukHire
-----------                                   -------
node scripts/export-legacy-recruitment.cjs    python -m app.legacy_import \
  --company <id> --out bundle.json              --bundle bundle.json \
  (READ ONLY transaction; mode 600)             --organization-id <org>   # dry run
                                                ... review the report and the quarantine ...
                                                --apply                   # commit
```

* **Organization mapping is explicit:** `--organization-id`, or `--create-organization NAME
  --admin-email EMAIL`; an unlinked workspace is linked to the company only with `--link-company`.
* **Dry run is the default** and executes the real import inside a transaction that is rolled back.
* **Idempotent:** every imported row is recorded in `legacy_import_map` with a snapshot of the source.
* **Never guessed:** unknown stages, missing parents, orphan scorecards, non-https resume URLs go to
  `legacy_import_quarantine` with the reason and original data.
* **Preserved:** resume URLs, scorecards (only where the candidate link exists), referral
  attribution, existing employee links (as `completed` handoffs, so HRMS is never asked for a second employee).
* **Reconciliation:** per table, source = imported + already imported + quarantined; non-zero exit if it does not balance.
* Legacy KukBook tables are **never modified or dropped** by any of this.

## 3. Cutover (per company) — prerequisites none of which this repo can verify

1. KukHire deployed (host, PostgreSQL, backups, TLS, `KUKHIRE_SECRET_ENCRYPTION_KEY`).
2. `KUKHIRE_AUTH_BASE_URL` set; identity host CORS allows the KukHire origin (UI uses direct login).
3. KukBook deployed with the handoff endpoint and migration; a `hr:write` key per company; host in `KUKHIRE_HRMS_ALLOWED_HOSTS_CSV`.
4. Export -> dry-run -> review quarantine -> apply -> reconcile -> test handoff on a test hire.
5. A KukBook company admin switches `recruitment_owner` to `kukhire` (needs `KUKHIRE_APP_URL` set there).

## 4. Merge order

1. KukBook PR (additive: endpoint, scopes, migration, switch default `legacy`; changes nothing for any company).
2. Deploy KukBook (manual, `sudo bash ~/deploy.sh`; expect all of `All migrations completed`, `health 200`, `DEPLOY OK`).
3. KukHire PR, then deploy KukHire.
4. Per-company cutover above.

## 5. Rollback

* Company: switch `recruitment_owner` back to `legacy` (nothing was deleted).
* KukHire data: discard the workspace; the importer only wrote to KukHire.
* Code: both PRs are additive and revertable; `hr_hire_handoffs` rows already written are history.
