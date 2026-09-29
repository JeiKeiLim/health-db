# job-9 retry 1 — TEST_BUG fix: no-truncation above 1000 observable in unit

- Commit: `7635505 tenet(job-9): assert no truncation above 1000 with 1100-row seed`
- No prior trial logs found for this job (glob of `*trial*.md` → none; prior
  job-9 commits `5c3b53c` / `c14ec48` were the implementation + journal).

## Fix (test_critic test bug + data_integrity_critic advisory)

1. `tests/unit/test_queries.py`: new `_seed_workout_corpus(pg, 1100)` + class
   `TestListWorkoutsNoTruncationAgainstPostgres` with ONE behavioral test
   `test_no_truncation_above_1000`: real-pipeline ingest of 1100 minimal
   workouts (synthetic UUIDs `…-{i:012d}`, same-day distinct start_ts),
   then `list_workouts(frm="2018-01-01", to="2018-12-31", limit=2000)`
   asserts `count == 1100` in a single page, `next_offset is None`,
   `limit == 2000` un-clamped, row list length 1100. A regression to the
   superseded hardcoded LIMIT 1000 now fails (count 1000 != 1100).
   Seeds via the module's existing `_lane_pg()` disposable postgres:18
   container pattern (same mechanism as the lane/sleep unit classes).
2. Advisory adopted: `ORDER BY w.start_ts DESC, w.id DESC` in
   `queries.list_workouts` (uuid tiebreak, total order determinism);
   existing response semantics unchanged — anti-scenario guard is
   acceptance TestScenario7 (3 passed) + all pagination unit tests.

## Mutation check (test observes the property)

Simulated `LIMIT LEAST(%s, 1000)` in queries.list_workouts →
`test_no_truncation_above_1000` FAILS with
`assert 1000 == 1100 — LIMIT 1000 regression would truncate this to 1000`;
source restored, test green against the real implementation.

## Gates

- `uv run pytest tests/unit/test_queries.py -q` — 86 passed
- `uv run pytest tests/acceptance/mcp_analysis_parity.py::TestScenario7WorkoutPagination -m requires_pg -q` — 3 passed
- `uv run ruff check src/ tests/` — All checks passed
- Scope discipline: only the two files touched; untracked
  `scripts/split_backfill.py` left unstaged.