# Research: PostgreSQL 18 schema design (partitioning, upserts, timezone) [scanned-not-verified]

Source: PostgreSQL 18 official docs §5.12 (fetched 2026-09-08), PG 18.6 live instance verified on <server-host>:5432.

## Key constraints discovered

1. **PK/unique on partitioned table MUST include the partition key** (docs §5.12.2.3). Therefore `raw_samples` (partitioned by RANGE (ts)) cannot have PK (metric_id, source_id, ts) — ts IS the partition key, so PK (metric_id, source_id, ts) DOES include it. ✅ Works because ts is in the key. But a pure (metric, source) uniqueness without ts is impossible — irrelevant here since ts is part of our natural key anyway.
2. **Generated columns cannot be partition keys** (immutability requirement). So: partition by `ts` directly; `day_kst` becomes a **stored generated column**: `GENERATED ALWAYS AS ((ts AT TIME ZONE 'Asia/Seoul')::date) STORED`. Wait — generated column expressions must be immutable; `AT TIME ZONE` with a named zone on a timestamptz is NOT immutable (zone rules change). Options:
   - a) fixed offset `+09:00` via `(ts AT TIME ZONE 'UTC' + interval '9 hours')::date` — `AT TIME ZONE 'UTC'` on timestamptz IS immutable → generated column OK. KST has no DST, so fixed offset is exact.
   - b) plain stored column computed in Python at ingest.
   - Decision: **(a) fixed-offset generated column** `(ts AT TIME ZONE 'UTC' + interval '9 hours')::date` — matches predecessor's KST semantics (system tz was Asia/Seoul; KST = UTC+9 fixed, no DST), zero per-row Python work, and day boundary semantics are identical.
3. **Declarative partitioning requires partitions to exist before insert** — no auto-create. Need a `ensure_partitions()` routine (create next-month partition ahead of time; called at ingest start; idempotent). Missing partition → insert error → 503-safe failure (payload archived).
4. `ON CONFLICT` works on partitioned tables (conflict target = the parent PK/index, must include partition key — satisfied).
5. `workouts.intensity` is numeric in JSON but TEXT in old SQLite schema — resolve as `real` in PG.
6. No TimescaleDB/PostGIS on the server instance — native PG only. Confirmed live.
7. Monthly partitions for ~3yr+ of data at (31M rows equivalent) is modest; planner handles thousands of partitions; monthly is the sweet spot for rollup-range queries and cheap future retention.

## Schema sketch (final decision in spec)

```
metric_dim  (metric_id smallint PK, name text UNIQUE, display_name text, canonical_unit text, created_at)
source_dim  (source_id smallint PK, name text UNIQUE, created_at)
raw_samples (metric_id smallint NOT NULL FK, source_id smallint NOT NULL FK, ts timestamptz NOT NULL,
             qty double precision NOT NULL, day_kst date GENERATED, ingested_at timestamptz,
             PRIMARY KEY (metric_id, source_id, ts)) PARTITION BY RANGE (ts)
daily_rollups (metric_id, day_kst, sum_qty, avg_qty, min_qty, max_qty, sample_count, updated_at, PK (metric_id, day_kst))
workouts (id uuid PK, name, start_ts, end_ts, day_kst date, duration_sec, active_energy_kcal, total_energy, basal_energy, mets, temperature, humidity, is_indoor bool, location, intensity real, distance_km, walking_running_distance_km, step_count, step_cadence, speed, avg_heart_rate, max_heart_rate, heart_rate_recovery jsonb, ingested_at)
workout_heart_rate (workout_id uuid FK CASCADE, ts timestamptz, heart_rate int, PK (workout_id, ts))
routes (workout_id uuid FK CASCADE, ts timestamptz, latitude, longitude, course, speed, elevation, h_acc, v_acc, PK (workout_id, ts))
raw_ingest (id bigserial PK, sha256 text UNIQUE, received_at, automation_name, automation_id, session_id, data_type, payload jsonb, outcome, error_note)
ingest_log / audit (failed auth: ts, ip, ua, reason)
schema_migrations (version, applied_at)
```

- `raw_ingest.payload` is jsonb (TOASTed, compressible) — verbatim archive in DB mirrors the disk archive; disk archive is written FIRST (archive-first safety).
- Rollups: day-scoped DELETE+INSERT inside the same transaction as sample upserts (parity with predecessor ingester.py:237-253).
- Migration tooling: plain versioned SQL files + a tiny Python applier (no alembic dependency) — matches "stdlib-first" spirit; alembic allowed if jobs prefer.

## psycopg3 notes

- Use `psycopg[binary]` + connection pool (psycopg_pool) for the FastAPI app; server-side parameter binding; ` executemany` for batch upserts; `ON CONFLICT (metric_id, source_id, ts) DO UPDATE SET qty = EXCLUDED.qty, ingested_at = EXCLUDED.ingested_at`.
- timestamptz: parse HAE `yyyy-MM-dd HH:mm:ss Z` with Python `datetime.strptime(..., '%Y-%m-%d %H:%M:%S %z')` → psycopg sends aware datetime.

Confidence: [scanned-not-verified] — partition-PK interaction and generated-column immutability verified against official docs; exact DDL to be proven in slice-2 migration job with tests.