# critic tailoring: 2 run-scoped critics added

type: journal
source_job: 00000000-0000-0000-0000-000000000000
job_name: unknown
created: 2026-09-08T01:46:53.580Z

## Findings

- **note**: Global 'security' critic was already disabled with a missing prompt file (.tenet/critics/security.md FILE MISSING) — pre-existing broken entry, left untouched (stays disabled). No orphaned run-scoped critics found (Step 0 no-op). Step 2 generated two run-scoped critics: ingest-security (secret exposure, auth gaps, injection, resource abuse on the exposed HTTP service) and data-integrity (idempotency, unit policy, KST day boundaries, archive-first, transactionality — the predecessor's three production data incidents as concrete risk classes). Both registered in .tenet/critics.json with full_context=true.
