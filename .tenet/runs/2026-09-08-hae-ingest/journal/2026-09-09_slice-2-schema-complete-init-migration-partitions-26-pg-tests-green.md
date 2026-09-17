# slice-2-schema complete: 0001_init + partitions, 26 pg tests green

**Job**: slice-2-schema (Database + MCP slice) · **Date**: 2026-09-09

## What was implemented

- `migrations/0001_init.sql` — full spec § Database Schema:
  - `metric_dim` (smallint identity PK, name UNIQUE, display_name, canonical_unit, is_cumulative), `source_dim`
  - `raw_samples` PARTITION BY RANGE (ts), PK (metric_id, source_id, ts), `day_kst` STORED generated column `((ts AT TIME ZONE 'UTC') + interval '9 hours')::date` (fixed +9h per research/postgres18-schema.md finding 2 — immutability empirically probed against PG 18 before authoring DDL)
  - `daily_rollups`, `workouts` (numeric intensity, ingested_at NOT NULL), `workout_heart_rate` + `routes` (FK CASCADE), `raw_ingest` (sha256 UNIQUE, payload jsonb), `auth_log` (+ ts DESC index), `raw_samples_day_kst_idx` for freshness/coverage reads
  - No partition DDL in the migration — partitions come from `db.partitions` on demand
- `src/health_db/db/partitions.py` — `ensure_partitions` (current + next month, `pg_advisory_xact_lock`, idempotent `CREATE TABLE IF NOT EXISTS`, `today` injectable), `ensure_backfill_range` (historical months for backfill), `month_range`/`partition_name` helpers
- `tests/unit/test_partitions.py` — 18 fake-conn unit tests (month math incl. Dec→Jan roll, lock-first ordering, IF NOT EXISTS shape, deterministic identical SQL on re-run, backfill range spans)
- `tests/acceptance/slice_2_schema.py` — 26 tests against real postgres:18: applied-schema state per harness rule (all 10 tables/column types/PKs/FKs/UNIQUE), generated day_kst boundary (14:59:59.999Z→09-08, 15:00:00Z→09-09), upsert ON CONFLICT latest-wins on the partitioned table, missing-partition insert failure, ensure_partitions on-demand + idempotent, backfill-range partitions accept inserts, rollup DELETE+INSERT recompute parity (ingester.py:237-253 shape), HR/route CASCADE, sha256 UNIQUE, jsonb roundtrip

## Verification

- Empirical pre-DDL docker probes confirmed: spec expression accepted as generated column; KST midnight boundary correct; partition + PK + ON CONFLICT latest-wins all work on PG 18
- `uv run pytest tests/acceptance/slice_2_schema.py` — 26 passed (16.5s, module-scoped container)
- `uv run pytest tests/acceptance/slice_2_db_config.py tests/acceptance/slice_2_schema.py -m requires_pg` — 33 passed
- Non-pg suite: 352 passed; `ruff check src/ tests/` clean
- Smoke: `health-migrate` entry point against disposable postgres:18 applied version 1; 10 tables present; `raw_samples` listed as partitioned table

## Notes for downstream jobs

- **psycopg placeholder parsing**: a literal `%` inside a query string (e.g. unit `'%'`) breaks parsing — always parametrize such values. Found during critic-style verification of my own tests.
- **pg_partitioned_table** joins on `partrelid` (not `ptrelid`).
- **Test-container economics**: function-scoped `pg` container per test = ~30 min for this module; slice_2_schema overrides it module-scoped with per-test TRUNCATE + partition-drop isolation. Same explicit skip contract.
- **conftest imports in test modules**: package-qualified `tests.acceptance.conftest` imports don't resolve in this pytest layout — use `importlib.util.spec_from_file_location` against the conftest path.
- Test-only fixes during verification (no production-code changes after first green): psycopg `%` placeholder, `partrelid`, partition pre-provisioning in raw_samples tests, `ingested_at` in the test workout insert.

## Unintended behavior check

None observed: migration is additive DDL only; no slice-1 surface touched (no-DB constraint intact — no pipeline/API changes in this job).