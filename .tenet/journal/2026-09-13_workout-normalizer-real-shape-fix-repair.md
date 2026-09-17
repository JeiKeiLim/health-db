# workout normalizer real-shape fix + repair

type: journal
source_job: ace96a6d-5cfa-4b44-8f0f-adcfddf80b25
job_name: interaction_e2e for 34b9c6a3
created: 2026-09-13T23:40:03.390Z

## Findings

- **after_db**: repair run 1: active_energy_kcal NOT NULL=27; workout_heart_rate=735; raw_samples=60000 unchanged; daily_rollups=400 unchanged; routes=19000 unchanged; raw_ingest=20 unchanged. Sample repaired row a1b2c3d4: active=190.0 kcal, total=235.0, basal=42.0, avg_hr=120.0, max_hr=155, steps=580, temp=27.0, humidity=65, intensity=5.9, recovery present.
- **before_db**: workouts=27 with active_energy_kcal NOT NULL=1; workout_heart_rate=1; raw_samples=60000; daily_rollups=400; routes=19000; raw_ingest=20.
- **commit**: dc4b065 tenet(fix-workout-normalizer): real HAE v2 workout shapes + jsonb write + force repair path
- **deviations**: Job was already partially implemented uncommitted by an interrupted prior attempt; verified/completed/committed it. `uv run pytest` (gate 1 literal) cannot launch due to repo venv whose console scripts carry a stale pre-move workspace path; used `uv run python -m pytest` instead after `chflags -R nohidden .venv` (documented cloud-sync workaround), venv untouched. Repair documented as `health-replay --dir <archive> --force` (`--repair` alias). Indoor workouts legitimately have null speed/cadence/distance in real payloads, so non-null asserted only where declared.
- **failure_evidence**: Old normalizer (git worktree at HEAD ac026e4) on tests/fixtures/workout_v2_sample.json: active_energy_kcal/total/basal/distance/avg_hr/max_hr/step_count/walking_running_distance = None, hr_rows=0, recovery=None (route=5 OK). New normalizer populates all. psycopg3 3.3.5 jsonb adaptation reproduced directly: ProgrammingError cannot adapt type 'dict' using placeholder '%s'; Jsonb wrap succeeds.
- **idempotency**: repair run 2 (--force) and run 3 (--repair) over data/archive: identical counts (27/735/60000/400/19000), no value drift. Plain non-force replay still reports force=false; sha gate exercised by test_plain_replay_does_not_reprocess_existing_sha.
- **scope_checked**: No edits under .tenet/project/**; .tenet/status/** and .critics.json orchestrator state left uncommitted/untouched.
- **tests_run**: uv run ruff check src/ tests/ -> clean; uv run python -m pytest tests/unit -> 450 passed; tests/acceptance -> 100 passed (pg against disposable postgres:18 containers; slice_2_db_mcp 42 passed).
