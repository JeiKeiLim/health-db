# slice-1-stats — retry #1: last_received_at concurrency regression fixed

Job: slice-1-stats (retry #1) · Commit: `3c8b92f` · Date: 2026-09-08

## What failed previously

code_critic finding on `src/health_db/stats.py:74-91`: `record_received` computed
the timestamp **outside** the lock and assigned `last_received_at`
**unconditionally**. Two concurrent ingest requests could interleave so that the
request which read the clock *earlier* acquired the lock *later* and regressed
`last_received_at` to an older timestamp.

## Fix (stats.py:74-97)

- Timestamp read + UTC normalization now happen **inside** `self._lock`.
- Monotonic guard: `last_received_at` is assigned only when it is `None` or the
  new stamp `>=` the current value (equal instants allowed — no regression,
  no lost update). `received` counter and automation-label semantics unchanged.

## Regression tests added (tests/unit/test_stats.py)

1. `test_out_of_order_call_never_regresses_last_received_at` — older pinned stamp after a newer one does not move the value.
2. `test_equal_timestamp_does_not_regress` — equal instant keeps the value.
3. `test_concurrent_out_of_order_stamps_never_regress` — 16 threads + `Barrier`, pinned stamps in reverse chronological order; final snapshot equals `max(stamps)`.
4. `test_interleaved_live_clock_calls_never_regress` — 4 threads × 50 live-clock calls; `last_received_at >= first` stamp.

## Acceptance (all verified)

- `uv run pytest tests/unit/test_stats.py` → **29 passed** (25 prior + 4 new)
- `uv run pytest tests/unit` → **179 passed**
- `uv run ruff check src/ tests/` → clean; `ruff format --check` clean
- Smoke check: uvicorn on :8765 with routes router — `/api/health` 200 `{"status":"ok"}`, stats 401 without/wrong token, 200 + 7-key snapshot with correct token

## Scope notes

- All prior verified work kept: 7-key snapshot contract (`SNAPSHOT_KEYS`), single-lock thread safety, token-guarded stats route, UTC/KST timestamp normalization.
- `health_db.main` is not implemented yet — it belongs to **slice-1-api**. The
  41 collection errors in `tests/acceptance/` (2 in slice_1_ingest.py, 39 in
  slice_2_db_mcp.py) are `ModuleNotFoundError: health_db.main` at fixture setup,
  pre-existing and unrelated to this job. Unit suite is the in-scope gate.