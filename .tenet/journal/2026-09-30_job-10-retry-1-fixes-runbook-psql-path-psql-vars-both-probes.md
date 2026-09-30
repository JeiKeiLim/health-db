# job-10 retry 1 fixes: runbook psql path, PSQL vars, both probes

type: journal
source_job: f0e7c4b8-5a24-4c2a-8c2a-9d1f0a2b3c4d
job_name: unknown
created: 2026-09-30T00:57:17.695Z

## Findings

- **commit**: f9d58d6 tenet(job-10): fix runbook exec path, PSQL_* vars, seed both probe metrics
- **fix_1_psql_exec_path**: README ops section + scripts/probe_cleanup.sql header no longer document docker-exec psql (runtime image python:3.12-slim-bookworm has no psql client). Documented host-side: PGPASSWORD="$PSQL_PASSWORD" psql "postgresql://${PSQL_USER}@${PSQL_URL}:${PSQL_PORT}/${PSQL_TABLE}" -v ON_ERROR_STOP=1 -f scripts/probe_cleanup.sql
- **fix_2_env_vars**: Standardized both docs and script header on pool.py keys (pool.py:44-48): PSQL_USER, PSQL_TABLE (dbname), PSQL_PASSWORD, PSQL_URL (host), PSQL_PORT. Grep clean of DATABASE_URL/PSQL_DATABASE_URL across README/scripts/src.
- **fix_3_runbook**: Job summary runbook text adopts the corrected host-psql command with -v ON_ERROR_STOP=1.
- **fix_4_test_seeding**: TestScenario8ProbeCleanup seeds BOTH __probe_metric (1 point) and __probe_metric2 (2 points), asserts both in names before cleanup, executes scripts/probe_cleanup.sql twice (idempotency), asserts both absent after. No vacuous DELETE assertions remain.
- **gates**: uv run pytest tests/acceptance/mcp_analysis_parity.py::TestScenario8ProbeCleanup -m requires_pg -q -> PASS; uv run pytest tests/unit/ -> 724 passed; uv run ruff check src/ tests/ -> All checks passed!
- **note**: Job prompt referenced class name TestScenario8; actual class in file is TestScenario8ProbeCleanup.
- **verification**: git diff reviewed for all 3 touched files; no scope drift - scripts/split_backfill.py left untracked, .tenet/** untouched.
