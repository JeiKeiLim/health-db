# migration 0002 additive-only lane tables

type: journal
source_job: 64f52d4c-f23c-4a8d-bfea-3bdf4a8177b2
job_name: Migration 0002: ECG + notification tables
created: 2026-09-17T05:51:14.212Z

## Findings

- **acceptance**: tests/acceptance/parse_everything_migration.py: module-scoped pg container (slice_2_schema pattern) + autouse _apply_migrations/_reset_data; 21 tests green in ~14s
- **anti_scenario_test**: tests/acceptance/parse_everything.py::TestAntiScenarios::test_migration_0002_additive_only passes (was passing before at HEAD too — file-level grep; my diff only reformatted long lines and fixed ruff)
- **caveat**: tests/acceptance/parse_everything.py still has 20 pre-existing expected failures (sleep rule, ECG/HRN lanes, conversions, MCP tools) — they define done for downstream jobs 2-8, not job-1
- **day_kst**: generated column expression copied verbatim from 0001_init.sql:50-51 with start_ts substituted for ts: (((start_ts AT TIME ZONE 'UTC') + interval '9 hours')::date) STORED — fixed UTC+9, immutable, PG18-accepted
- **deliverable**: migrations/0002_parse_everything.sql — additive-only CREATE TABLE for ecgs, ecg_voltages, heart_rate_notifications, heart_rate_notification_samples per spec §6
- **pytest_marker**: registered 'parse_everything' marker in pyproject.toml (was unregistered — PytestUnknownMarkWarning since commit 985df09)
- **ruff**: clean on src/ tests/ migrations (pre-existing E501s in parse_everything.py fixed mechanically: line wrapping + unused json import + trailing newline only; no assertion or fixture-semantics changes)
- **schema_verified**: disposable postgres:18: apply_all -> [1,2]; re-apply no-op; information_schema columns match spec types exactly; UNIQUE (source_name, start_ts) on both parents; PKs (ecg_id, seq) and (notification_id, ts, kind); cascade FKs verified; day_kst fixed UTC+9 verified (15:30Z -> next KST day); upsert ON CONFLICT latest-wins + child PK re-append rejection verified
- **unit**: tests/unit/test_migrations.py gains TestMigration0002File (docker-free): discovery [1,2], every statement is CREATE TABLE for exactly the 4 spec tables, no ALTER/DROP, verbatim execution through apply_all via FakeConn
