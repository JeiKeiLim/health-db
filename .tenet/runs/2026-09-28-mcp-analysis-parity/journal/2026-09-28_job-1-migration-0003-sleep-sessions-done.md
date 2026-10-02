# job-1 migration 0003 sleep sessions done

type: journal
source_job: 86202ea1-afa2-48e9-a18c-49aeb8fba2f0
job_name: Migration 0003 sleep_sessions
created: 2026-09-28T09:26:01.103Z

## Findings

- **attempt_history**: retry0: bundled scripts/split_backfill.py into job commit (scope_conflict from code_critic — ruff lint gate forced bundling); retry1: restored script to untracked, rewrote job commit to pyproject.toml-only 91e6a17, scoped ruff to src/ tests/ for verification. All 6 critics passed.
- **commit**: 91e6a17407026705cd6973cc41511c473a85dee1
- **danger_zones_held**: migrations/0001_init.sql and 0002_parse_everything.sql untouched; sleep_shape_matrix.json untouched; test_census_lock.py untouched
- **job_id**: job-1
- **lesson_for_local_tier**: include an explicit 'do NOT stage unrelated untracked files (e.g. scripts/split_backfill.py); scope ruff to src/ tests/' line in dev job prompts
- **migration**: 0003_sleep_sessions.sql additive-only: sleep_sessions table (16 cols, UNIQUE(source_name,sleep_start), GENERATED day_kst fixed UTC+9 family from 0001, day_kst index), registered in versioned runner with pins [1,2,3]
- **verified**: 17/17 migration tests on real postgres:18, 26 original acceptance tests still collected, 190 total default collection, ruff clean
- **worker_retries**: 2
