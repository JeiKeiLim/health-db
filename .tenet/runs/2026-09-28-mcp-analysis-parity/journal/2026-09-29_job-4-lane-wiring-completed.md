# job-4 (retry 1): pipeline sleep-session lane — completed

Date: 2026-09-29 · Job: job-4 (pipeline _upsert_sleep_session_lane + replay census)

## What landed

1. **Commit of the working tree** (previous attempt was complete but uncommitted):
   `src/health_db/pipeline.py` — `_upsert_sleep_session_lane` (dataplane:
   normalizer → batched UPSERT_SLEEP_SESSION executemany, skips merged into
   the same per-payload transaction; census comment updated).
2. **replay census propagation**: `src/health_db/replay.py` — docstring +
   stderr `skipped-points` diagnostics now include `sleep_session=` alongside
   `sleep_no_value=` (zero-silent-drops made observable across a backfill).
3. **Fake-txn handler**: `tests/unit/test_lane_persistence.py`
   `_FakeTxn.HANDLERS` gains `("INSERT INTO sleep_sessions", "_sleep_sessions")`
   mirroring the ecg/hrn latest-wins handler (13 params, key
   (source_name, sleep_start), snapshot/rollback coverage incl.).
4. **New unit suite** `tests/unit/test_sleep_session_lane.py`
   (TestSleepSessionLanePersistence, 5 tests) exercising the REAL
   ingest_payload against the fake pool:
   - spec §1 Watch night lands one row, 13 fields verbatim (asleep: 0
     sentinel stored 0.0, never derived);
   - same (source_name, sleep_start) re-posted with changed stages → single
     row refreshed (DO UPDATE latest-wins; count never grows);
   - force re-read of the identical payload rewrites the same row;
   - point lacking parseable sleepStart → no row + skipped.sleep_session == 1;
   - lane rows OUT of daily_rollups (session-lane isolation).
   Test-module imports resolve `tests.*` via `pythonpath = ["."]` in
   pyproject (test runner now sees the repo root).
5. **Acceptance seam fix**: `tests/acceptance/mcp_analysis_parity.py`
   `test_replay_idempotent` — `api.route_archive_root()` (invented API) →
   `archive.route_archive_root` property on the conftest `Archive` seam
   (fixture arg added). The replay walk now runs; the test's remaining
   failure is the job-5 query layer, not the lane.

## Verification (honest run log)

- `uv run pytest tests/unit/` → **680 passed** (incl. the 5 new lane
  persistence tests + census lock + replay units, all green).
- `uv run ruff check src/ tests/` → **All checks passed!**
- `uv run pytest tests/acceptance/mcp_analysis_parity.py -m requires_pg`
  (docker up) → 7 passed / 19 failed, **all 19 out-of-scope-pending**:
  - TestScenario1 x2, TestScenario2 x3 (incl. fixed seam test),
    TestScenario4 x1, TestScenario9 x1 → `AttributeError:
    module health_db.queries has no attribute list_sleep_sessions`
    (job-5 read layer not implemented yet)
  - TestScenario5 x4 → TypeError: get_workout_detail() got an unexpected
    keyword argument 'route_offset' (job-7)
  - TestScenario6 x3 → freshness latest_activity_day (job-8)
  - TestScenario7 x3 → list_workouts pagination (job-9)
  - TestScenario8 x1 → probe-cleanup SQL (job-10; its embedded BEGIN/COMMIT
    also cannot run inside one psycopg prepared statement — job-10 should
    split the statement)
  - TestMcpToolSurface.test_schema_resource x1 → health://schema lacks the
    sleep_sessions entry (job-6)
  - job-4-observable acceptance tests that DO pass: TestScenario9
    .test_skip_counted (skipped.sleep_session via real /api/ingest),
    TestScenario4.test_existing_sleep_analysis_untouched,
    TestAntiScenarios x4 (census lock unchanged, no rollup contamination,
    response fields preserved, skipped-keys additive).

## Doctrine drift

None observed — pipeline/replay conventions matched the harness contract.