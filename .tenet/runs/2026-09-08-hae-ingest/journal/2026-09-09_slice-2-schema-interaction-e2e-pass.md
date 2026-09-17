# slice-2-schema interaction-e2e: library surface verified on real postgres:18

**Job**: slice-2-schema (interaction_e2e critic) · **Date**: 2026-09-09

## Verdict

PASS. Attempt-2 commit `bb4ee3f` contains exactly the two code-critic fixes; verified
behaviorally, no further product changes needed (eval worker: no code edits, no re-commit).

## Layer 1 — scripted (declared)

- `uv run pytest tests/unit -q` → **329 passed**
- `uv run pytest tests/acceptance -m "slice1 or slice2" --ignore=tests/acceptance/slice_2_db_mcp.py` → **56 passed**
  (slice_2_schema 26 + slice_1_ingest 23 + slice_2_db_config 7, all on real postgres:18)
- `uv run ruff check src/ tests/` → **All checks passed!**
- Full `-m "slice1 or slice2"` run shows 26 ERRORs — all `fixture 'api'/'mcp' not found` in
  `tests/acceptance/slice_2_db_mcp.py`, a scaffold-era stub (untouched since da9499b) whose
  fixtures belong to slice-2-ingest-pipeline/-replay/-mcp per conftest.py:12-13. OUT OF SCOPE.

## Layer 2 — exploratory (library surface, disposable postgres:18, ~40 checks)

- Chained workflow: apply_all → ensure_partitions → insert → re-run both = full no-op.
- ensure_backfill_range hostile inputs all correct: inverted range → [], single-day,
  single-month, Dec→Jan year boundary, 3-month chronological, far-past 1999-06,
  overlap on existing partition (no dup), 12-month chain in one call.
- ensure_partitions(today=date(2026,12,15)) injected → p2026_12+p2027_01; repeated → same.
- Error path: duplicate-range CREATE rejected loudly (psycopg DuplicateTable); catalog unchanged after failed DDL.
- day_kst fixed +9h across year boundary (2024-12-31 23Z → 2025-01-01) via generated column.
- Concurrency: 4 threads racing ensure_partitions via separate pools — all succeed, identical names, no extra partitions (advisory lock holds).
- month_range contract: Dec 31, leap Feb 29, Jan 1, mid-month normalization.
- health-migrate CLI (real process): rc=0 first run "applied now: 1"; idempotent re-run rc=0 "applied now: none"; bad --migrations-dir rc=1 generic secret-free message (no traceback); --help works.
- migrations.discover: duplicate version raises MigrationError; non-.sql ignored; 10-digit version ordered correctly; missing dir raises; apply_all missing dir = documented no-op.
- Iron law 1: DbSettings repr/redacted hide password; invalid PSQL_PORT warns + falls back.
- **Non-vacuity (finding 1) proven**: injected a rogue raw_samples_p2030_01 partition →
  pg_inherits/pg_class assertion mechanism detects it → rewritten test is genuinely
  catalog-sensitive; rogue partition dropped afterwards.

## Notes

- 2 initial probe FAILs were probe-script bugs, not product bugs (DuplicateTable vs
  DuplicateObject expectation; temp-dir cwd resolution for health-migrate).
- No stray containers; temporary eval probe container removed.
