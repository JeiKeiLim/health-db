# job-7 pipeline wiring complete all critics pass

type: journal
source_job: 909384a9-f0d6-465d-866d-69a8b7c5dbc5
job_name: Pipeline wiring: lane upserts in one transaction
created: 2026-09-19T14:34:06.314Z

## Findings

- **commits**: ["ee2d133","46e3265","2a64b82","886980d","49d26d2"]
- **job**: job-7
- **lesson**: Pipeline wiring landed: UPSERT_ECG/UPSERT_ECG_VOLTAGE/UPSERT_HRN/UPSERT_HRN_SAMPLE in db/helpers.py:97-146 (latest-wins ON CONFLICT, RETURNING id, batches <=10000 via ECG_VOLTAGE_BATCH); _upsert_ecg_lane/_upsert_hrn_lane in ingest_payload same transaction (pipeline.py:406-412); lane skip census folded into skipped dict. ECG voltage VALUE overflow now guarded (isfinite + OverflowError/OSError/ValueError, normalize.py:1016-1027) closing the job-5 residual. ENVELOPE SPEC AMENDMENT RATIFIED BY ORCHESTRATOR: lane-only payloads (ecg/heartRateNotifications with no metrics/workouts) now accepted 200 — supersedes spec section-2 'envelope stays as-is' line; data_type reconciled via shared _classify_data_type (both /api/ingest response and raw_ingest); drift journaled. Real multi-batch test (10003 voltages, chunks [10000,3]) guards the 600k backfill. Side effects: queries.py _SCHEMA_TABLES done early (job-8 deliverable partially landed) + schema_resource pool kwarg shim. 586 unit tests green; TestEcgLane+TestNotificationLane 6/6; remaining 2 parse_everything failures = TestMcpTools (job-8 scope, expected).
- **retries**: 1 (multi-finding: voltage overflow, data_type, envelope ratification, batching test)
- **run**: 2026-09-17-normalizer-audit
- **status**: complete
