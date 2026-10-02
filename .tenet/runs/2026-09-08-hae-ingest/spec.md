---
delivery_mode: agile
---

# Spec: HAE Ingest — server PostgreSQL health data platform

Run: `.tenet/runs/2026-09-08-hae-ingest/` · Repo: health-db (greenfield, new home; replaces predecessor)

## Purpose

Build a self-hosted ingest + query platform: the Health Auto Export (HAE) iOS app POSTs JSON v2 exports to a FastAPI service on the self-hosted server, which archives every payload verbatim (archive-first), normalizes, and stores into PostgreSQL 18.6 (`health_app@<server-host>:5432`). Queries are served by an MCP-native streamable-HTTP server (tools + resources, token auth) with a thin CLI wrapper for dev testing. History is backfilled via HAE itself — no SQLite migration.

## Tech Stack (pinned)

| Component | Choice |
|---|---|
| Language | Python 3.12+, uv-managed (`pyproject.toml`), ruff lint, pytest |
| HTTP framework | FastAPI + uvicorn (ASGI) |
| DB driver | psycopg3 (`psycopg[binary]` + `psycopg_pool`) — plain SQL, no ORM |
| MCP server | official `mcp` SDK v2 (`mcp>=2,<3`), streamable-http transport |
| Database | PostgreSQL 18.6 (<server-host>:5432, db/role `health_app`, single account, UTF8/UTC server) |
| Deployment | Docker image (single service) on Docker → existing postgres container; local dev via local postgres container |
| Tests | pytest; real postgres container for integration tests (testcontainers or compose); SDK `Client` for MCP e2e |

## API Endpoints

### Ingest (HAE → service)

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/api/ingest` | `X-Ingest-Token` header (name configurable via env `INGEST_HEADER`, default `X-Ingest-Token`; value = `INGEST_SECRET`) | Accept HAE JSON v2 payload. Steps: auth check → read body → verbatim archive to disk → content-hash check → parse+validate → (slice 1: report only / slice 2: upsert to PG) → respond |
| GET | `/api/health` | none | Liveness: `{"status":"ok"}` (+ DB connectivity in slice 2) |
| GET | `/api/ingest/stats` | `INGEST_SECRET` | Counts: payloads received/stored/duplicate/quarantined, last received at (observability for the user during backfill) |

### Failure semantics (archive-first: nothing is ever lost)

| Mode | Behavior |
|---|---|
| Bad/missing token | 401; audit log entry (ts, ip, ua, reason); body NOT archived (never trusted) |
| Malformed JSON / wrong shape | 400; payload archived to `quarantine/` with error note; counted in stats |
| Valid JSON, unknown metric | accepted; metric auto-registered in `metric_dim` (slice 2) / reported as unknown-but-accepted (slice 1) |
| Duplicate payload (same sha256) | 200 idempotent, `{"outcome":"duplicate"}`; no reprocessing |
| DB unavailable (slice 2) | 503 + `Retry-After: 300`; payload archived to disk (NOT raw_ingest) → replay tool re-processes when DB returns |
| Oversized body (> `MAX_PAYLOAD_BYTES`, default 64 MiB) | 413; not archived |
| Successful store | 200 `{"outcome":"stored", "metrics": n, "workouts": m, "samples": k}` in < 2s |

### MCP (query surface — slice 2)

| Aspect | Detail |
|---|---|
| Transport | streamable-http at `/mcp` (separate uvicorn app/port from ingest, or ASGI-mounted — implementer's choice per research/mcp-python-sdk.md) |
| Auth | `Authorization: Bearer <INGEST_SECRET>` (middleware; 401 otherwise) |
| Tools | `query_day(date, metric?)`, `get_metric_series(metric, from, to)`, `list_metrics()`, `list_workouts(from, to)`, `get_workout_detail(id)` (HR + routes), `get_trends(metric, window)`, `freshness_check()`, `raw_samples(metric?, from?, to?, limit, offset)` (limit default 1000, max 10000) |
| Resources | `health://metrics` (catalog: name, unit, coverage, first/last day), `health://freshness`, `health://schema` (table/column summary) |
| Output | JSON-structured, paginated where unbounded, summaries by default; errors are actionable messages ("metric 'X' not found — call list_metrics") |

### CLI wrapper (dev testing — slice 2)

`uv run health <command>` mirroring core MCP tools (query-day, metrics, workouts, trends, freshness, raw, routes): JSON to stdout, diagnostics to stderr. Not the product surface; thin shared query-layer.

## Database Schema

Conventions: all timestamps `timestamptz` (UTC); `day_kst` = KST calendar day (fixed UTC+9, no DST); FKs enforced; migrations versioned SQL in `migrations/` applied by a small Python applier at service start (`health-migrate` entry point).

### metric_dim
| Column | Type | Constraints |
|---|---|---|
| metric_id | smallint | PK, GENERATED ALWAYS AS IDENTITY |
| name | text | UNIQUE NOT NULL (HAE snake_case name, e.g. `step_count`) |
| display_name | text | NOT NULL (CamelCase display, e.g. `Step Count`) |
| canonical_unit | text | NOT NULL (SI/metric canonical unit; see unit policy) |
| is_cumulative | boolean | NOT NULL — seeded from unit: true iff canonical_unit ∈ CUMULATIVE_UNITS {"count","km","kcal","kJ","min","hr","mg","g","mcg"} (parity with predecessor constants.py:14, where trends choose sum_qty vs avg_qty); pass-through metrics (no canonical map entry) seed from their declared HAE `units` value |
| created_at | timestamptz | NOT NULL DEFAULT now() |

### source_dim
| Column | Type | Constraints |
|---|---|---|
| source_id | smallint | PK, GENERATED ALWAYS AS IDENTITY |
| name | text | UNIQUE NOT NULL (source may be omitted in payload → `'unknown'`) |
| created_at | timestamptz | NOT NULL DEFAULT now() |

### raw_samples (PARTITION BY RANGE (ts), monthly)
| Column | Type | Constraints |
|---|---|---|
| metric_id | smallint | NOT NULL FK → metric_dim |
| source_id | smallint | NOT NULL FK → source_dim |
| ts | timestamptz | NOT NULL (sample date; PK includes partition key) |
| qty | double precision | NOT NULL |
| day_kst | date | GENERATED ALWAYS AS (((ts AT TIME ZONE 'UTC') + interval '9 hours')::date) STORED |
| ingested_at | timestamptz | NOT NULL DEFAULT now() |
| | | **PK (metric_id, source_id, ts)** |

Upsert: `INSERT ... ON CONFLICT (metric_id, source_id, ts) DO UPDATE SET qty = EXCLUDED.qty, ingested_at = EXCLUDED.ingested_at` (latest-wins — correct for overlapping "Standard" date-range re-sends and corrected values). Partition maintenance: `ensure_partitions()` creates current + next month at ingest; idempotent.

### daily_rollups
| Column | Type | Constraints |
|---|---|---|
| metric_id | smallint | NOT NULL FK; PK (metric_id, day_kst) |
| day_kst | date | NOT NULL |
| sum_qty | double precision | NOT NULL |
| avg_qty | double precision | NOT NULL |
| min_qty | double precision | NOT NULL |
| max_qty | double precision | NOT NULL |
| sample_count | bigint | NOT NULL |
| updated_at | timestamptz | NOT NULL |

Recomputed per affected day inside the ingest transaction: `DELETE FROM daily_rollups WHERE day_kst = ANY($days); INSERT ... SELECT ... FROM raw_samples WHERE day_kst = ANY($days) GROUP BY metric_id, day_kst` (parity with predecessor ingester.py:237-253).

### workouts
| Column | Type | Constraints |
|---|---|---|
| id | uuid | PK (HAE workout UUID) |
| name | text | NOT NULL |
| start_ts / end_ts | timestamptz | NOT NULL |
| day_kst | date | NOT NULL (KST day of start_ts) |
| duration_sec | double precision | NOT NULL |
| active_energy_kcal | double precision | (v2 JSON kJ ÷ 4.184 at normalize) |
| total_energy / basal_energy | double precision | |
| mets | double precision | |
| temperature / humidity | double precision | |
| is_indoor | boolean | |
| location | text | |
| intensity | double precision | (JSON numeric; fixes old SQLite TEXT/numeric mismatch) |
| distance_km / walking_running_distance_km | double precision | |
| step_count | bigint | |
| step_cadence / speed | double precision | |
| avg_heart_rate / max_heart_rate | double precision | |
| heart_rate_recovery | jsonb | |
| ingested_at | timestamptz | NOT NULL |

### workout_heart_rate
`workout_id uuid FK→workouts ON DELETE CASCADE, ts timestamptz, heart_rate int, PK (workout_id, ts)`

### routes
`workout_id uuid FK→workouts ON DELETE CASCADE, ts timestamptz, latitude double precision NOT NULL, longitude double precision NOT NULL, course, speed, elevation, h_acc, v_acc double precision, PK (workout_id, ts)`

### raw_ingest (payload archive in DB; disk archive written first)
| Column | Type | Constraints |
|---|---|---|
| id | bigserial | PK |
| sha256 | text | UNIQUE NOT NULL (duplicate skip) |
| received_at | timestamptz | NOT NULL |
| automation_name / automation_id / session_id | text | from HAE headers |
| data_type | text | ('metrics' | 'workouts') |
| payload | jsonb | NOT NULL (verbatim) |
| outcome | text | NOT NULL ('stored' | 'duplicate' | 'replayed') |
| error_note | text | |

### auth_log (audit)
`id bigserial PK, ts timestamptz NOT NULL, ip text, user_agent text, reason text NOT NULL`

### schema_migrations
`version int PK, applied_at timestamptz NOT NULL`

## Unit & normalization policy (ported from predecessor, single REST lane)

- Canonical units (constants.py:104-119): kg / % / m / km / km/hr / m/s / cm / kcal / degC per metric; **non-canonical-unit samples are skipped** (not converted); the only conversion is energy kJ→kcal ÷ 4.184 (workout + JSON metric energy). Metric→unit map seeded into `metric_dim.canonical_unit`; metrics not in the map accept any unit (pass-through). **Blood-pressure systolic/diastolic split is NEW behavior authored from spec + official format docs — predecessor has no blood_pressure handling to port (readiness validator finding, 2026-09-08).**
- Metric naming: store HAE snake_case `name` in `metric_dim.name`; `display_name` for humans. No cross-lane collisions possible (single lane).
- Special formats (normalize rules): heart_rate → qty = `Avg`; blood_pressure → qty = `systolic` + separate `diastolic` sample rows under derived metric names (`blood_pressure_systolic`, `blood_pressure_diastolic` — display "Blood Pressure Systolic/Diastolic"); sleep_analysis aggregated → one row per day: qty = `asleep` if present else `core+deep+rem` (asleep-preference, AutoSleep wins; parity with TASK-005); unaggregated sleep → qty from segment with `value` recorded in source_dim? No — `value` recorded to `source_dim` is wrong; store as-is with qty (documented behavior; aggregation ON is the recommended automation setting so this is the edge case).
- Workouts: v2 fields per research/hae-json-v2-format.md; kJ→kcal for energies; heartRateData → workout_heart_rate rows; route → routes rows; upsert by UUID (latest-wins on re-send).
- Day boundaries: `day_kst` from ts via fixed +9h offset (KST has no DST; parity with Asia/Seoul semantics).

## Design Direction

Agile-mode visuals in `.tenet/runs/2026-09-08-hae-ingest/visuals/`: `2026-09-08-00-architecture.html` (final-product architecture, all slices), `2026-09-08-01-final-product.html` (final state of ingest/MCP/CLI surfaces), `2026-09-08-02-slice-1-ingest-validation.html` and `2026-09-08-03-slice-2-database-mcp.html` (per-slice wireframes), `2026-09-08-04-prototype-slice-walkthrough.html` (interactive slice walkthrough reviewed at the plan-checkpoint). Run-local design decisions: `.tenet/runs/2026-09-08-hae-ingest/design.md`. Doctrine: `.tenet/project/design.md` — terminal/JSON is the interface; LLM-centric MCP output shaping (catalog-first, pagination, actionable errors) is the product's "UX".

## Auth Flow

1. HAE automation configured with header `X-Ingest-Token: <INGEST_SECRET>` (env-provided, `openssl rand -hex 32`).
2. Service compares constant-time (`hmac.compare_digest`); on mismatch → 401 + auth_log row; no archive.
3. MCP clients send `Authorization: Bearer <INGEST_SECRET>`; same constant-time check in middleware; 401 otherwise.
4. `.env` (gitignored) holds `PSQL_*` + `INGEST_SECRET` + `INGEST_HEADER`; `.env.example` documents keys. Secrets never logged (auth_log stores IP/UA/reason only).

## HAE App Configuration (user setup; documented in README)

Three REST automations (JSON, v2, Batch Requests ON, `X-Ingest-Token` header, timeout 60s):
1. **Metrics-all** — all metrics, Summarize ON, hours grouping, Date Range "Standard", hourly cadence
2. **Metrics-HR+Steps** — Heart Rate + Step Count only, minutes grouping, hourly cadence (disjoint from #1)
3. **Workouts** — Include Workout Metrics ON (minutes), Include Route Data ON, daily cadence

Backfill: Manual Export per-year chunks (JSON v2) through the same endpoint; idempotent upserts make overlap safe; ≤ 1 day total. Captured payloads from slice 1 double as replay fixtures for development.

## Success Criteria

1. Slice 1 use-checkpoint: real HAE app POSTs accepted with 200 < 2s; 100% payloads archived verbatim under `data/archive/` (organized by date); quarantine + 401 + stats paths demonstrable via curl; `GET /api/health` returns ok.
2. Backfill: full history loads through ingest endpoint within 1 day; re-posting any chunk yields zero double-counts (idempotency proven by re-running a chunk and comparing row counts).
3. Hourly delta syncs land within 5 minutes of the hour (observed via raw_ingest.received_at over the post-deploy validation).
4. MCP: all 8 tools + 3 resources work over streamable-http with Bearer auth; rollup-level query < 1s for 90-day range (measured on server instance with EXPLAIN ANALYZE / timing in test); unauthenticated call → 401.
5. Rollups parity: for captured real payloads, daily_rollups in PG match Python-recomputed expectations from the same payloads (and spot-check vs predecessor SQLite for overlapping days during validation).
6. Zero unexplained 4xx/5xx during a post-deploy validation of live syncs (stats endpoint shows counts; any 4xx/5xx accounted for as quarantine/413/401 tests).
7. Test suite: unit (normalizer, unit policy, day computation) + integration (ingest→PG against real postgres container, partition creation, rollup recompute, idempotency) + MCP e2e (SDK Client) all green via `uv run pytest`; `ruff check` clean.

## Out of Scope

- Analysis features (stress, training load, correlations) — deferred
- SQLite data migration — history comes from HAE sync only
- Multi-user/multi-device (single data subject; `?target=` per-device separation not needed)
- TLS termination & port-forwarding (deploy behind reverse proxy later; service itself stays plain HTTP on LAN)
- TimescaleDB/PostGIS (not available on instance; native PG only)
- Grafana/dashboards; predecessor retirement (post-cutover, separate effort)
- HAE TCP-server direction (app-as-server / MCP against the app) — unrelated to this pipeline

## Slice plan

Total slices: 2

### Slice 1: Ingest validation with real data (no DB)
- **Adds**: Working REST ingest service accepting real HAE payloads: token auth + audit log, verbatim disk archive (`data/archive/YYYY/MM/DD/`), content-hash duplicate detection, payload validation/reporting (metrics/workouts/samples counted, unknown formats reported), quarantine for malformed, `/api/health`, `/api/ingest/stats`, local dev setup (uv, Dockerfile, compose with local postgres stubbed-out env), pytest suite (unit + HTTP integration via TestClient), README with HAE automation setup instructions.
- **Bundled with**: project scaffolding (pyproject, ruff, CI-ready test commands), `.env.example`, capture-format documentation.
- **User can**: configure the 3 HAE automations on iPhone pointing at the dev machine (LAN), trigger Manual Exports + hourly syncs, watch payloads arrive via stats endpoint, and trust every payload is archived for slice-2 development.
- **Out of slice**: PostgreSQL writes (except optional local-dev postgres container readiness), normalization into DB, MCP server, CLI.

### Slice 2: Database + MCP query surface + deploy
- **Adds**: PG schema + migrations + partition maintenance; full ingest pipeline (normalize → upsert → rollup recompute) with replay tool for slice-1 captured payloads + DB-outage queue; MCP streamable-http server (8 tools, 3 resources, Bearer auth); thin CLI wrapper; Docker deployment to server; backfill runbook (Manual Export chunks); post-deploy validation procedure.
- **Bundled with**: performance verification (rollup query < 1s @ 90d), replay-based integration fixtures, README updates (MCP client config examples).
- **User can**: replay captured history into server PG, run full-history backfill from the phone, connect MCP HTTP clients (Claude/other) to query their health data, and observe hourly deltas landing.
- **Out of slice**: TLS/port-forward setup (user's later manual step; runbook documented), analysis features, predecessor decommission.