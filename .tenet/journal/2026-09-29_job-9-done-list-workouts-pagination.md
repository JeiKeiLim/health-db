# job-9 done: list_workouts pagination

type: journal
source_job: 00000000-0000-0000-0000-000000000000
job_name: unknown
created: 2026-09-29T18:49:31.803Z

## Findings

- **acceptance_results**: TestScenario7WorkoutPagination 3/3 passed under requires_pg (walk 100/100/40, markers [100,200,None], total 240; default 500; empty range next_offset null). Full parity file: 25 passed / 1 failed — the failure is TestScenario8ProbeCleanup (job-10's multi-command BEGIN/DELETE/COMMIT SQL hits psycopg prepared-statement single-command limit), verified pre-existing via git stash BEFORE this job. Other acceptance files: 68 (db_mcp+parse_everything) + 21 (migration) + 49 (slice_1+slice_2 incl. perf) green. Full unit suite 716 passed. ruff check src/ tests/ clean. Full suite run: 905 passed / 1 failed (same pre-existing job-10 test). Commit 5c3b53c.
- **anti_scenario_7_untouched**: test_anti_response_fields_preserved + census lock green; list_workouts response changes are strictly additive (count kept, id/name/start/end/day/duration/energies/hr_points/route_points rows unchanged).
- **deliverable**: queries.list_workouts(frm, to, limit=None, offset=0, pool=None) — signature renames start/end→frm/to (the lane-list convention AND the contract TestScenario7 already imports: acceptance calls frm=/to=); limit clamps [1,2000] via _bounded_limit (None=500 default), offset via _validated_offset; LIMIT %s OFFSET %s fetch limit+1 → has_more trims lookahead row; response {workouts, count(page rows), limit, offset, next_offset(offset+limit iff has_more else None)}. New constants WORKOUT_DEFAULT_LIMIT=500 / WORKOUT_MAX_LIMIT=2000 next to the route/sleep families. MCP tool list_workouts passes limit/offset through (schema test pins from/to aliases + paging props); CLI workouts gains --limit/--offset (COMMAND_OPTIONS table drives handler + help); README roster + CLI example updated.
- **notes_for_next_job**: job-10 (probe-cleanup) must fix TestScenario8's SQL execution: psycopg3 conn.execute with BEGIN;...;COMMIT; in one string fails ('cannot insert multiple commands into a prepared statement') — use a script() call or autocommit conn with separate statement executes. Deploy ordering unchanged: replay first, then cleanup.
