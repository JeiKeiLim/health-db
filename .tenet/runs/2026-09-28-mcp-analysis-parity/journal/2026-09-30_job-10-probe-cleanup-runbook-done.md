# job-10 probe cleanup runbook done

type: journal
source_job: 8ed70b09-243a-49c8-8174-ba43758ab170
job_name: Probe-cleanup runbook script
created: 2026-09-30T01:03:36.589Z

## Findings

- **attempt_history**: retry0: code_critic FAIL — runbook used docker exec psql (runtime image has no psql client), env var names inconsistent (DATABASE_URL vs PSQL_DATABASE_URL; contract is PSQL_* per pool.py:44-48), TestScenario8 vacuous for __probe_metric2; retry1: all four fixed (f9d58d6 committed via fidelity critic context), runbook now host-side psql -v ON_ERROR_STOP=1, Scenario8 seeds both probes
- **commit**: be34ceb (script + fixes) + f9d58d6 (retry fixes)
- **job**: job-10 probe-cleanup runbook script
- **job_id**: job-10
- **lesson**: ops runbooks must be executed verbatim during eval (interaction_e2e ran the README command live) — docs that reference container tooling must match the image reality; env var names must come from pool.py as the single source
- **worker_retries**: 1
