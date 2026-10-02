# interaction-e2e job-8ed70b09 probe-cleanup runbook live pass

type: journal
source_job: ebb3e5ab-dc5f-4ec2-aa13-041996d5882d
job_name: interaction_e2e for 8ed70b09
created: 2026-09-30T01:03:17.042Z

## Findings

- **acceptance**: uv run pytest tests/acceptance/mcp_analysis_parity.py -m requires_pg → 26 passed in 68s (disposable pg:18 container); TestScenario8ProbeCleanup::test_probe_metrics_gone_after_cleanup → 1 passed standalone. Non-vacuous seed verified by source: test asserts both '__probe_metric' AND '__probe_metric2' present via list_metrics before executing the SQL file verbatim, then absent after (tests/acceptance/mcp_analysis_parity.py:454-474) — each DELETE provably matches rows.
- **live_runbook**: Verified the VERBATIM README.md command (README.md:245-248 = scripts/probe_cleanup.sql:14-16, both agree on PGPASSWORD + postgresql://${PSQL_USER}@${PSQL_URL}:${PSQL_PORT}/${PSQL_TABLE} -v ON_ERROR_STOP=1 -f scripts/probe_cleanup.sql) against a live disposable postgres:18.6 Docker container (host 127.0.0.1:15599, homebrew psql 16.13 client, python:3.12-slim no-psql premise respected since client ran on host). Chain: health-migrate (migrations 1,2,3) → real uvicorn ingest (:8221, /api/health db:true) → real HTTP POST /api/ingest with both probe metrics → {"outcome":"stored","metrics":2,"samples":3} → pre-cleanup list_metrics shows both probes → runbook command output: BEGIN / DELETE 3 / DELETE 3 / DELETE 2 / COMMIT / EXIT=0 → post-cleanup list_metrics has no probes, freshness_check empty → idempotent re-run DELETE 0/0/0 EXIT=0 → container force-removed. ON_ERROR_STOP makes failure exit non-zero per psql -v semantics.
- **previous_attempt**: Previous attempt produced narrative-only output with no rubric JSON; this rerun ends with the rubric.
- **scripted_gates**: tests/unit/test_probe_cleanup_script.py 7/7; ruff check src/ tests/ scripts/ clean.
- **verdict**: passed
