# interaction-e2e job-3 surface library upsert probe pass

type: journal
source_job: 355465e4-e25c-4388-bd5e-40ed2d52d487
job_name: interaction_e2e for ad6604a9
created: 2026-09-29T03:02:14.140Z

## Findings

- **deliverable**: test_sleep_session_upsert_updates_every_payload_column (tests/unit/test_lane_persistence.py:424, commit 5f6b1a4)
- **evidence**: {"mutation_analysis":"11/11 col=EXCLUDED.col drop-entries killed; test logic catches exactly the mutation class the test critic flagged","requires_pg_acceptance":"mcp_analysis_parity_migration.py 17/17 PASS on disposable postgres:18","scope_hygiene":"commit touches ONLY tests/unit/test_lane_persistence.py; scripts/ left untracked; ruff src/ tests/ clean","unit_suite":"tests/unit/test_lane_persistence.py 26/26; target test green in isolation","upsert_probe":"helpers.UPSERT_SLEEP_SESSION + migrations 0001..0003 against live pg:18 container — INSERT stores all 13 payload cols; UPDATE on (source_name, sleep_start) conflict rewrites all 11 payload cols to new values, same id RETURNING, 2 upserts->1 row, ingested_at refreshed, zero sentinels verbatim"}
- **notes**: mcp_analysis_parity.py has 20/26 acceptance failures — ALL trace to job-4/5/7/8/9/10 deliverables not yet built (list_sleep_sessions, route_archive_root, pipeline lane, skip key in response); test docstring declares this intentional (TDD staged acceptance). job-3 must not be failed for downstream absence. record_date column is timestamptz (not date): probes comparing date-strings must compare instants instead.
- **surface**: library (spec §232: no human-facing UI; MCP tools + CLI are the interaction surfaces; job-3 deliverable is a SQL constant + unit test)
- **verified**: true
