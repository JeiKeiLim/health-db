# job-8 freshness latest activity day done

type: journal
source_job: 7dfac2b6-a28f-41c5-a350-4794a7758634
job_name: freshness latest_activity_day
created: 2026-09-29T18:00:00.356Z

## Findings

- **commit**: 9f7d8ce (orchestrator fallback commit — 4th uncommitted-tree occurrence)
- **delivers**: additive latest_activity_day = max(last_day) over ACTIVITY_DAY_METRICS (7 exact metrics), missing ignored, None when none, empty-DB branch carries key; latest_complete_day/stale untouched; TestScenario6 3/3 pass; 713 unit pass
- **env_incident**: sleep_session_fidelity critic pruned docker anonymous volumes and rm -f'd dev container health-db-postgres-1 (restored via docker compose up -d postgres, pgdata volume intact). Note the recurring disk-full pattern: anonymous volumes from disposable test containers accumulate ~120GB — periodic docker volume prune is needed
- **job**: job-8 freshness latest_activity_day
- **job_id**: job-8
- **lesson**: keep watching docker VM disk usage; also workers keep leaving trees uncommitted — the per-job prompt DOES mandate commit but workers still skip it; fallback commit each time is working
- **worker_retries**: 0
