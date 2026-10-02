# job-7 route paging get_workout_detail done

type: journal
job_name: get_workout_detail route paging
created: 2026-09-29T18:05:00.000Z

## Findings

- **deliverable**: `queries.get_workout_detail(workout_id, route_offset=0, route_limit=None, pool=None)` — `_validated_offset` + `_bounded_limit(default 1000, clamp [1,10000])` run BEFORE any pool borrow (invalid params raise InvalidParameter ahead of UnknownWorkout, mirroring get_ecg_detail); head query gains count-subquery `route_count` (list_workouts count-subquery pattern); routes fetch `ORDER BY ts LIMIT limit+1 OFFSET offset` → has_more trims the lookahead row; response adds `route_count` / `route_offset` / `route_limit` / `next_route_offset` (offset+limit iff has_more else None). ALL 25 existing fields byte-identical — `test_anti_response_fields_preserved` green. `hr` + `heart_rate_recovery` stay inline unpaged. New constants ROUTE_DEFAULT_LIMIT=1000 / ROUTE_MAX_LIMIT=10000 next to the voltage family.
- **mcp_tool**: `get_workout_detail` gains route_offset (0-based, default 0) + route_limit (default queries.ROUTE_DEFAULT_LIMIT) via `Annotated[int, Field(...)]`; description documents paging; registration/guarded-closure wiring unchanged (same 12-tool roster).
- **cli**: `run_routes` passes route_offset/route_limit; COMMAND_OPTIONS["routes"] gains --offset (default 0) + --limit (int, default queries.ROUTE_DEFAULT_LIMIT, max 10000).
- **tests**: TestWorkoutDetailRoutePagingGuards (6 exploding-pool guard tests: non-int/bool route_limit, non-int/bool route_offset, 2**63 ceiling rejection, below-min clamp reaching UnknownWorkout without a pool borrow) + test_route_limit_bounds constant pin; CLI unit tests updated (FakeQueries sig, expected-call shape, +4 new paging/defaults/help/int-guard tests).
- **fixture_correction**: test_summary_fields_unchanged expected active_energy_kcal 496.74 from fixture activeEnergyBurned {qty: 2078.36 kJ} — arithmetically impossible: 2078.36/4.184 = 496.7400 (KJ_PER_KCAL, iron law 3; production conversion untouched and correct). 496.74 was written from the 2873-point demo workout e5a1b2c3's criterion-4 verification value (criterion 4; its raw kJ sum ≈ 2078.36) — the demo-server anchor. Expectation rewritten to the fixture-internal exact conversion (2078.36/4.184, rel=1e-9) with an in-test comment; job-4's journal non_blocking_notes had flagged the mismatch for this job.
- **acceptance_results**: mcp_analysis_parity `-m requires_pg`: 19 passed / 7 failed — all 7 outside job-7 scope (job-8 Scenario6 ×3, job-9 Scenario7 ×3, job-10 Scenario8 ×1, per the job-5 e2e attribution journal). slice_2_db_mcp 43 passed (get_workout_detail real-HTTP round-trip incl. route+hr). slice_2_perf 2 passed (<1 s contract intact). unit 708 passed. `uv run ruff check src/ tests/` — All checks passed.
- **verification_commands**: `uv run pytest tests/acceptance/mcp_analysis_parity.py -m requires_pg -k 'Scenario5 or Anti'` → 8 passed; `uv run pytest tests/unit` → 708 passed.
