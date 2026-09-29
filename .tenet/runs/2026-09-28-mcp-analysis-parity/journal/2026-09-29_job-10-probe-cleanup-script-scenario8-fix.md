# job-10 probe cleanup script + Scenario8 fix

type: journal
source_job: 00000000-0000-0000-0000-000000000000
job_name: unknown
created: 2026-09-29T23:29:41.000Z

## Findings

- **acceptance**: tests/acceptance/mcp_analysis_parity.py 26/26 passed (-m requires_pg); unit 747 passed (-m 'not requires_pg'); ruff clean (src/ tests/ scripts/)
- **behavior**: psycopg3 raises psycopg.errors.SyntaxError 'cannot insert multiple commands into a prepared statement' when conn.execute() gets a multi-statement BEGIN/DELETE/COMMIT block with parameters. Acceptance tests running a multi-statement SQL script must read the .sql file and execute it without params (cur.execute(text)) — matching the real psql -f invocation.
- **deliverable**: scripts/probe_cleanup.sql — transactional (BEGIN/COMMIT) cleanup of __probe_metric/__probe_metric2 from daily_rollups -> raw_samples -> metric_dim (catalog deleted last), idempotent (absent-row DELETE is a no-op), NOT a migration. Header documents the mandatory ordering: must run AFTER 'health-replay --dir data/archive --force' (deploy sequence step 3) because replay would resurrect probe rows from archived payloads.
- **doctrine_drift**: none observed
- **spec_delta**: TestScenario8 was pre-written with parameterized multi-statement SQL that cannot execute under psycopg3; corrected to run the deliverable script file verbatim, with a second execution to prove re-run idempotency. Script-content unit tests added at tests/unit/test_probe_cleanup_script.py (transaction shape, table order, probe names, subquery scoping, no ALTER/DROP/UPDATE/TRUNCATE, replay-ordering doc present).
- **test_files**: tests/unit/test_probe_cleanup_script.py (new, 7 tests); tests/acceptance/mcp_analysis_parity.py TestScenario8ProbeCleanup (rewritten to execute scripts/probe_cleanup.sql)
