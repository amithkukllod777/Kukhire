# Hire handoff: KukHire -> KukHRMS

**Ownership.** KukHRMS creates and owns the employee, the onboarding checklist, payroll,
attendance and leave. KukHire never writes an HRMS table and never reads HRMS's database. KukHire
records *that* a hire was handed off and *which employee reference* came back.

## Flow

1. An application reaches `hired` (a person moved it; nothing automated can).
2. A workspace admin/recruiter calls `POST /v1/organizations/{org}/applications/{id}/handoff`.
3. KukHire sends `POST {hrms_base_url}/api/v1/hr/hires` (KukBook) with the workspace's **company API
   key** (scope `hr:write`), stored encrypted (Fernet, `KUKHIRE_SECRET_ENCRYPTION_KEY`).
4. HRMS creates the employee in one transaction with its idempotency claim and answers
   `201` (created) or `200` (replay). KukHire stores the returned employee reference and marks the
   handoff `completed`.

## Payload (minimum necessary)

`externalApplicationRef` (`kukhire:<application id>`, the idempotency key), `name`, optional `email`,
`jobTitle`, `department`, `startDate`, `referrerEmployeeId`, `legacyCandidateId` (set only for
imported candidates, so HRMS adopts an existing employee link instead of creating a second one).
**No salary** - compensation is an HRMS/payroll decision.

## Guarantees

* **Authorization is the credential.** HRMS takes the company from the API key, never from the body.
* **Idempotent.** Retrying (or two clicks at once) cannot create two employees; the unique claim
  `(company, source, externalRef)` is inserted in the same transaction as the employee.
* **Honest status.** `pending` -> `completed` only on a real `2xx`. A rejection (`4xx`) is `failed` with
  `rejected: <reason>` (HTTP 422 to the caller); a transient failure (`5xx`/network) is `failed` with
  `retryable: ...` (HTTP 502, "safe to retry" - the stable reference makes the retry idempotent).
  Nothing reports success it did not receive.
* **SSRF-safe target.** The HRMS URL is admin-supplied, so it must match
  `KUKHIRE_HRMS_ALLOWED_HOSTS_CSV` (exact host[:port]), must be https (unless the dev-only flag is
  set), redirects are not followed, and the API key is never logged.
* **Referral:** KukHire keeps *attribution* (who referred). Approving or paying a referral bonus
  is HRMS/payroll's (`recruitment.listHandoffReferrals` / `setHandoffReferralBonus` in KukBook).

## Setup (per workspace)

1. KukBook company admin creates an API key with scope `hr:write` (and `hr:read` to reconcile).
2. KukHire admin: `PUT /v1/organizations/{org}/integrations/hrms` with the base URL and key.
3. Ensure the host is in `KUKHIRE_HRMS_ALLOWED_HOSTS_CSV`.

## Verification status

Contract tested on both sides with synthetic data: KukBook's 23 handoff tests (MariaDB 10.11,
including concurrent requests) and KukHire's handoff tests against a fake HRMS. Real
KukHire -> real KukBook endpoint over HTTP: `tests/e2e/test_real_hrms.py` (opt-in; see the file).
**Not verified:** production MySQL, any production host.
