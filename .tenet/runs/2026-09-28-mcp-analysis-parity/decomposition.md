# Decomposition for mcp-analysis-parity (2026-09-28)

delivery_mode: autonomous · model_tier: **local** (fine-grained DAG: more,
smaller, single-responsibility jobs with explicit per-job acceptance
criteria — a weaker executor gets a tighter plan).

Acceptance tests live at `tests/acceptance/mcp_analysis_parity.py`
(26 tests, pre-written; they define done). Full suite green +
`ruff check src/ tests/` clean is the run-level gate.

## ASCII DAG

```
job-1  migration 0003 (sleep_sessions DDL)
   │
job-2  SKIPPED_KEYS + normalizer _sleep_session_rows (pure)
   │
job-3  db/helpers UPSERT_SLEEP_SESSION (SQL constant)
   │
job-4  pipeline _upsert_sleep_session_lane (transaction wiring)
   │
job-5  queries.list_sleep_sessions (read layer)
   │
job-6  MCP tool list_sleep_sessions + CLI health sleep + schema resource
   │
   ├──────────────┐
   │               │
job-7  route      job-8  freshness
   paging (P2)       latest_activity_day (P2)
   │               │
   └───────┬───────┘
           │
job-9  list_workouts pagination (P3)
           │
job-10 probe-cleanup SQL runbook script (P3 ops)
           │
   e2e-1   INTEGRATION CHECKPOINT (report-only):
           full suite + ruff + acceptance walk + additive-only audit
```

Jobs 1→6 are strictly sequential (each consumes the previous contract).
Jobs 7, 8 are independent of job-6's output AND of each other (both touch
only queries.py read layer + CLI/MCP wiring) but depend on job-6 only for
merge hygiene in shared files — they are registered with depends_on
["job-6"] to serialize edits to queries.py/mcp_server.py/cli.py and avoid
conflicts (local-tier caution; the shared-file surface makes parallel
edits risky).

## Job Details

### job-1 — Migration 0003_sleep_sessions.sql
- type: dev · depends_on: []
- Deliverable: `migrations/0003_sleep_sessions.sql` — additive-only
  (CREATE TABLE + UNIQUE (source_name, sleep_start) +
  sleep_sessions_day_kst_idx), day_kst generated fixed UTC+9 (same family
  as 0001 raw_samples), one transaction. Registered by the migration
  runner (check how 0002 registers in `src/health_db/db/migrations.py`).
- Acceptance: `uv run pytest tests/acceptance/parse_everything_migration.py -m requires_pg`
  still green (0001/0002 untouched); new migration applies on a fresh
  schema; `information_schema` shows sleep_sessions columns/types per spec
  table; file contains no ALTER/DROP on existing tables.
- Danger zones: never touch 0001/0002.

### job-2 — SKIPPED_KEYS extension + normalizer session rows
- type: dev · depends_on: [job-1]
- Deliverable: in `src/health_db/payloads.py` add `"sleep_session"` to
  SKIPPED_KEYS (additive, exactly once). In `src/health_db/normalize.py`
  add pure `_sleep_session_rows(parsed)` (or sibling of metric_rows_detailed)
  returning (rows, skips): recognizes exactly the aggregated-sleep shapes
  (date + ∩ _SLEEP_FIELD_KEYS), extracts all fields verbatim
  (source_name, record_date, sleep_start, sleep_end, in_bed_start,
  in_bed_end, total_sleep, asleep, deep, core, rem, awake, in_bed);
  sleepStart unparseable/absent → no row + skip count under
  `sleep_session`. NO changes to the existing sleep branch emissions.
- Acceptance: new unit tests in tests/unit (e.g. test_normalize.py or a
  new test_sleep_sessions.py): the 5 sleep_shape_matrix.json shapes each
  emit the right session row or skip; sentinel `asleep: 0` verbatim;
  NO_SLEEP_START shape → skip counted; census lock
  tests/unit/test_census_lock.py GREEN UNTOUCHED;
  test_anti_skipped_keys_additive passes.
- Danger zones: sleep_shape_matrix.json frozen; existing branch emissions
  unchanged (census lock is the guard).

### job-3 — UPSERT_SLEEP_SESSION SQL constant
- type: dev · depends_on: [job-2]
- Deliverable: `UPSERT_SLEEP_SESSION` in `src/health_db/db/helpers.py`
  next to UPSERT_ECG/UPSERT_HRN: INSERT ... ON CONFLICT (source_name,
  sleep_start) DO UPDATE SET (all payload columns + ingested_at), RETURNING
  id. Parameterized, no string interpolation.
- Acceptance: unit test asserting the SQL text shape (conflict target,
  DO UPDATE, RETURNING id) mirroring how helpers constants are covered
  today; `uv run ruff check src/` clean.

### job-4 — Pipeline lane wiring
- type: dev · depends_on: [job-3]
- Deliverable: `_upsert_sleep_session_lane` in `src/health_db/pipeline.py`
  following `_upsert_ecg_lane` (pipeline.py:239): normalize → upsert each
  row (executemany acceptable — no child rows), merge skips via
  merge_skipped into the SAME per-payload transaction (ingest_payload);
  include the session count in the ingest response accounting the same way
  ecg/notification lanes report.
- Acceptance: acceptance tests TestScenario1/2/4/9 (post → list → values,
  reingest no duplicates, skip counted in response) pass under
  `-m requires_pg`; replay force idempotency test passes
  (TestScenario2.test_replay_idempotent).
- Notes: `replay.py` accounting may need the new skip key in its run-wide
  census — mirror how ecg/notification counters propagate
  (replay.py:94, :418).

### job-5 — queries.list_sleep_sessions
- type: dev · depends_on: [job-4]
- Deliverable: `list_sleep_sessions(frm="", to="", limit=None, offset=0,
  pool=None)` in `src/health_db/queries.py` mirroring `list_ecgs`
  (queries.py:659) + raw_samples paging convention (queries.py:908-967):
  day_kst range where via _day_range_where, ORDER BY sleep_start DESC,
  _bounded_limit(…, 100, 500), _validated_offset, fetch limit+1 →
  next_offset None on last page, rows with _ts_iso timestamps, response
  {sessions, count, limit, offset, next_offset}.
- Acceptance: unit tests for clamp/marker logic (no docker) +
  acceptance tests that already call queries.list_sleep_sessions pass
  under requires_pg.

### job-6 — MCP tool + CLI + schema resource
- type: dev · depends_on: [job-5]
- Deliverable: MCP tool `list_sleep_sessions(from?, to?, limit?, offset?)`
  in `src/health_db/mcp_server.py` (mirror list_ecgs tool registration:
  schema, handler wrapping queries.list_sleep_sessions). CLI
  `health sleep --from --to --limit --offset` in `src/health_db/cli.py`
  (mirror health routes command). `health://schema` resource entry for
  sleep_sessions (find the schema table list in queries.py — the
  tests assert it). README tool roster + CLI examples update.
- Acceptance: TestMcpToolSurface tests pass; CLI round-trip unit test in
  tests/unit/test_cli.py (mirror existing command tests); mcp acceptance
  fixture round-trip if one exists for tools (slice_2_db_mcp.py pattern).

### job-7 — get_workout_detail route paging
- type: dev · depends_on: [job-6]
- Deliverable: add `route_offset=0, route_limit=None` params to
  queries.get_workout_detail; clamp [1, 10000] default 1000 via
  _bounded_limit/_validated_offset; add route_count (existing
  count(*) subquery pattern), route_offset, route_limit,
  next_route_offset; keep ALL existing fields byte-identical. CLI
  `health routes <id> --offset --limit` gains the same flags.
- Acceptance: TestScenario5 all 4 tests pass (first page + marker,
  full walk 1000/1000/1000/99, summary fields unchanged, default 1000);
  test_anti_response_fields_preserved passes (no field removed).

### job-8 — freshness latest_activity_day
- type: dev · depends_on: [job-6]
- Deliverable: in queries.freshness_check + the health://freshness
  resource builder: compute max(last_day) over exactly
  sleep_analysis, heart_rate_variability, resting_heart_rate, step_count,
  active_energy, apple_exercise_time, walking_running_distance; missing
  metrics ignored; None when none exist. latest_complete_day/stale
  semantics untouched.
- Acceptance: TestScenario6 passes (max over present metrics, missing
  ignored, resource carries field); existing freshness tests still green.

### job-9 — list_workouts pagination
- type: dev · depends_on: [job-7, job-8]
- Deliverable: queries.list_workouts gains limit (default 500, clamp
  [1,2000]) + offset (validated); response adds limit/offset/next_offset
  (None on last page); keep count. MCP tool list_workouts passes the new
  optional params through. CLI flags if the workouts command exposes them.
- Acceptance: TestScenario7 passes (walk 100/100/40 + markers 100/200/
  None, default 500, empty range); no silent truncation above 1000.

### job-10 — probe-cleanup runbook script
- type: dev · depends_on: [job-9]
- Deliverable: a small script `scripts/` (or documented psql heredoc in
  the run journal + README ops section) executing the spec §5 transactional
  DELETE SQL, safe to re-run (idempotent — deleting absent rows is a
  no-op). Runs AFTER backfill replay in the deploy sequence — document
  the ordering prominently.
- Acceptance: TestScenario8 passes under requires_pg (probe present →
  cleanup → gone); script content matches spec §5 SQL; README notes the
  ordering (replay first, then cleanup).

### e2e-1 — Integration checkpoint (report-only)
- type: integration_test · report_only: true · depends_on: [job-10]
- Deliverable: run the full verification battery and REPORT (no fixes):
  `uv run pytest` (all), `uv run ruff check src/ tests/`,
  `uv run pytest tests/acceptance/mcp_analysis_parity.py -m requires_pg`
  (if docker available; note skips explicitly otherwise),
  additive-only audit (0001/0002 diff-free, sleep_shape_matrix.json
  byte-identical, SKIPPED_KEYS superset), census lock green.
- Escalates via tenet_report_blocking_finding if anything fails.

## Interface Contracts (between jobs)

- job-2 → job-3: `SleepSessionRow` dataclass (slots, frozen — mirror
  EcgRow in normalize.py) with fields: source_name, record_date,
  sleep_start, sleep_end, in_bed_start, in_bed_end, total_sleep, asleep,
  deep, core, rem, awake, in_bed. Timestamps parsed via _parse_ts.
- job-3 → job-4: UPSERT_SLEEP_SESSION SQL constant taking the row fields
  in a fixed parameter order, RETURNING id.
- job-4 → job-5: sleep_sessions table populated per ingest payload
  transactionally; skipped.sleep_session in every ingest response.
- job-5 → job-6: list_sleep_sessions(frm, to, limit, offset) →
  {sessions, count, limit, offset, next_offset}; rows dict per spec §1.
- job-6 → jobs 7/8/9: queries.py/mcp_server.py/cli.py conventions
  (bounded-limit helpers, _ts_iso, tool registration shape) reused
  verbatim — the later jobs are pattern-copies of the job-5/6 surfaces.

## Local-tier notes

- Every job is sized for one session: ≤ 2 files of real logic + tests.
- Each prompt must carry: the spec section, the exact file(s) to touch,
  the mirror-pattern file to copy from, and the test command(s) to run.
- Workers must NOT touch files outside their deliverable list (danger
  zones in harness.md); shared-file edits are serialized by the chain
  job-6 → job-7 → job-8 → job-9 to avoid merge conflicts.