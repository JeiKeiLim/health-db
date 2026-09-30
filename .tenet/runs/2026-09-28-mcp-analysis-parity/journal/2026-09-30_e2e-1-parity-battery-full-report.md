# e2e-1 parity battery full report

type: journal
source_job: fdc9839c-8a1c-4193-9c38-02a1d50519ff
job_name: Integration: full parity battery
created: 2026-09-30T03:44:19.276Z

## Findings

- **anti_scope_scans**: no ALTER/DROP in 0003 (comment substring only); archive.py/pending.py/.tenet/project diff vs run start: empty
- **battery**: ["1. uv run pytest: 914 passed / 0 failed / 0 skipped (351.61s)","2. uv run pytest -m requires_pg: 167 passed / 0 skipped / 0 failed (305.09s); conftest spun disposable postgres:18 per test; docker daemon available — no faked results","3. uv run ruff check src/ tests/: All checks passed","4. additive-only audit: git diff dd668f8..HEAD and working tree both empty for migrations/0001_init.sql + 0002_parse_everything.sql; sleep_shape_matrix.json byte-identical (0-line diff); SKIPPED_KEYS == (unit_unconvertible, unrecognized_shape, ecg, notification, sleep_no_value) + sleep_session — superset holds, exactly one additive key","5. tests/unit/test_census_lock.py: 4 passed","6. tests/acceptance/mcp_analysis_parity.py: 26/26 tests collected and passed in 04:11 — S1 x2, S2 x3, S4 x2, S9 x2, S5 x4, S6 x3, S7 x3, S8 x1, anti x5, tool-surface x3; none skipped","7. rollup leak: TestScenario4.test_anti_session_rows_never_in_rollups joins daily_rollups to metric_dim on name LIKE 'sleep_session%' and asserts 0; green. Pipeline lane (_upsert_sleep_session_lane, pipeline.py:348) writes only via UPSERT_SLEEP_SESSION — no rollup path; recompute_rollups consumes only metric-lane day buckets"]
- **conclusion**: all 7 report items pass; no policy/integrity/coverage defects found in reviewed output; no blocking finding filed
- **job**: e2e-1 report-only integration checkpoint for run 2026-09-28-mcp-analysis-parity (job_id fdc9839c-8a1c-4193-9c38-02a1d50519ff)
- **notes**: one anti-pattern confirmed absent: probes not resurrected — job-10 retry 1 seeded both probe metrics then cleaned them (journal 2026-09-30_job-10-retry-1); data_integrity lens: zero-silent-drops enforced via skipped.sleep_session in SKIPPED_KEYS census
