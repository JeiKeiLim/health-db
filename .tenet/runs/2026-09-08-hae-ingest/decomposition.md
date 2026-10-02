# Decomposition for hae-ingest

Delivery mode: **agile** · Model tier: **local** (fine-grained, single-responsibility jobs with explicit acceptance criteria) · Slice 1 fired 2026-09-08 after plan-checkpoint approval.

## Slice 1: Ingest validation with real data (no DB)

Scope: spec § "Slice plan → Slice 1" + scenarios 1/2 (slice-1 part) + anti-scenarios 2/5. Acceptance tests: `tests/acceptance/slice_1_ingest.py` (`-m slice1`).

```
slice-1-scaffold ──┬── slice-1-config ──┬── slice-1-auth ──┐
                   │                    │                  ├── slice-1-archive ──┬── slice-1-api ──► slice-1-e2e
                   └── slice-1-payloads ─┴── slice-1-stats ─┴── slice-1-quarantine┘        (integration_test, report_only)
```

### Job details

- **slice-1-scaffold** (`dev`, no deps) — uv project scaffolding: `pyproject.toml` (python 3.12, fastapi, uvicorn, pytest, ruff, httpx; `mcp`/`psycopg` deferred to slice 2), `src/health_db/__init__.py`, `tests/` layout, ruff config (line 100), `.gitignore` (data/, .env), `.env.example` (PSQL_*, INGEST_SECRET, INGEST_HEADER, MAX_PAYLOAD_BYTES). Acceptance: `uv run pytest` collects (0 errors), `uv run ruff check src/ tests/` clean, package imports.
- **slice-1-config** (`dev`, deps: scaffold) — `src/health_db/config.py`: env-driven settings (ARCHIVE_ROOT default `data/archive/`, INGEST_SECRET, INGEST_HEADER default `X-Ingest-Token`, MAX_PAYLOAD_BYTES default 64 MiB) with a small `Settings` dataclass + `from_env()`; unit tests for defaults/overrides. Acceptance: unit tests green; no hardcoded secrets anywhere.
- **slice-1-payloads** (`dev`, deps: scaffold) — `src/health_db/payloads.py`: HAE JSON v2 parse + validate (`data.metrics[]`, `data.workouts[]`); sample counting (metrics groups, data points, workouts); classify data_type; unknown-format detection (report, don't reject); special shapes recognized per research/hae-json-v2-format.md (qty/Min-Avg-Max/systolic-diastolic/sleep aggregated/unaggregated). Pure functions, no I/O. Acceptance: unit tests covering every documented shape + unknown-metric + wrong-shape rejection; `took_ms` not required here.
- **slice-1-auth** (`dev`, deps: config) — token middleware/dependency: constant-time compare (`hmac.compare_digest`), configurable header name, 401 JSON body (no stack trace, no secret), in-memory audit counter (ts, ip, ua, reason; NEVER the token/body). Acceptance: unit tests — missing/wrong token → 401; correct token passes; audit increments; secret absent from response.
- **slice-1-archive** (`dev`, deps: config) — `src/health_db/archive.py`: verbatim write BEFORE parse outcome (`data/archive/YYYY/MM/DD/HHMMSS-sha8.json`), atomic write (temp + rename), quarantine subdir for malformed, content sha256 computation, `find_by_sha256()` duplicate check, in-memory index of seen hashes (slice 1 has no DB). Iron law: payload on disk before any processing verdict; untrusted (401) bodies never archived. Acceptance: unit tests — byte-identical archive, atomicity (no partial files on simulated failure), quarantine path, duplicate detection.
- **slice-1-stats** (`dev`, deps: config) — `/api/health` ({"status":"ok", version}) + `/api/ingest/stats` (received/stored/duplicate/quarantined/last_received_at/last_automation/unauthorized_attempts; token-protected) backed by a small in-memory counters singleton. Acceptance: unit tests for counters + protection.
- **slice-1-quarantine** (`dev`, deps: archive) — malformed/wrong-shape handling: 400 + quarantine id in response + error note stored alongside quarantined file; oversize → 413 (before archive, before body read fully); malformed-but-authorized → always quarantined (anti-scenario 2). Acceptance: unit tests per failure mode (b"{broken", {"nope":1}, oversized).
- **slice-1-api** (`dev`, deps: auth, archive, stats, quarantine, payloads) — `src/health_db/main.py`: FastAPI app factory wiring the pipeline: auth → read body (cap) → sha256 → duplicate? → archive → parse → 200 {"outcome","data_type","metrics","samples","workouts","unknown_formats","archive_path","sha256"} (<2s not asserted at unit level); duplicate → 200 {"outcome":"duplicate"}; HAE automation headers (automation-name/id/aggregation/period, session-id) surfaced to stats.last_automation. Acceptance: `uv run pytest -m slice1` green end-to-end against TestClient; `uv run health-db` serves on :8000 (entry point added).
- **slice-1-e2e** (`integration_test`, report_only, deps: api) — Run the full slice-1 acceptance suite (`uv run pytest tests/acceptance -m slice1`) + `ruff check`; verify outcomes not absence of errors; report pass/fail per test. Does NOT fix code; uses tenet_report_blocking_finding for blockers.

### Interface contracts (slice 1)

- `config.Settings` — frozen dataclass consumed by auth/archive/api; test override via monkeypatch (fixture contract in slice_1_ingest.py).
- `archive.ArchiveStore` — `store(body, *, quarantine: bool) -> Path`, `sha256_of(body)`, `seen(sha) -> bool`, `mark_seen(sha)`; filesystem layout `archive/YYYY/MM/DD/`, `archive/quarantine/`.
- `payloads.parse(body) -> PayloadReport` (dataclass: data_type, metrics, samples, workouts, unknown_formats) or raises `MalformedPayload`.
- `stats.Stats` — process-wide singleton: `record_received/stored/duplicate/quarantined/unauthorized(...)`, `snapshot() -> dict`.
- No DB import anywhere in slice 1 (grep-able constraint).

### Slice-1 use-checkpoint (after slice-1-e2e passes)
User configures 3 HAE automations on iPhone → real payloads → stats endpoint shows stored/duplicate counts → captured corpus in `data/archive/` becomes slice-2 fixtures.
## Slice 2: Database + MCP query surface + deploy

Fired 2026-09-09 after slice-1 use-checkpoint `approve`. Scope: spec § "Slice plan → Slice 2" + scenarios 1 (slice-2 part)/3/4/5 + success criteria 2/3/4/7. Acceptance tests: `tests/acceptance/slice_2_db_mcp.py` + `tests/acceptance/slice_2_perf.py` (added by deploy job), run additively with `-m "slice1 or slice2"`.

```
slice-1-e2e (complete) ──┐
                         │
   slice-2-db-config ──► slice-2-schema ──┐
   (psycopg3 pool, PSQL_*    (migrations/  │
    config, pg container    SQL, ensure_  ├─► slice-2-ingest-pipeline ──► slice-2-replay
    fixture, migration       partitions)   │   (normalize→upsert→         (health-replay:
    applier)                │             │    rollup tx, 503+pending,   dir replay + pending
   slice-2-normalize ───────┘             │    workouts+HR+routes,       queue, idempotent)
   (pure unit policy +                     │    raw_ingest, api fixture)      │
    special formats,                       │         │                        │
    day_kst +9h)                            │         ▼                        │
                                           └─► slice-2-mcp-server ──► slice-2-cli
                                               (query layer, 8 tools,    (health CLI
                                                3 resources, Bearer,     wrapper)  │
                                                mcp fixture, SDK Client)  │        │
                                                                    ┌───────┴────────┘
                                                                    ▼
                                                          slice-2-deploy ──► slice-2-e2e
                                                    (Dockerfile, compose,    (integration_test,
                                                     runbook, README,        report_only)
                                                     perf test <1s @90d)
```

### Job details

- **slice-2-db-config** (`dev`, deps: slice-1-e2e) — DB foundation: `src/health_db/db/` package — psycopg3 (`psycopg[binary]` + `psycopg_pool`) connection pool from PSQL_* env (never server prod in tests; iron law 7), migration applier (`schema_migrations` table, versioned SQL files in `migrations/`, `health-migrate` entry point, idempotent), `pg` pytest fixture spinning a disposable postgres:18 container (docker CLI or compose; `INTEGRATION_TEST_DB` URL; create/drop own schema; skip-if-no-docker with explicit reason), `/api/health` extended with DB connectivity check. Acceptance: pool connects to container; migrate up twice = no-op; pg fixture works; unit+slice-1 suites stay green.
- **slice-2-schema** (`dev`, deps: db-config) — `migrations/0001_init.sql`: metric_dim (with is_cumulative seed rule, CUMULATIVE_UNITS parity), source_dim, raw_samples PARTITION BY RANGE (ts) with stored generated day_kst (fixed +9h, immutable expression per research/postgres18-schema.md), daily_rollups, workouts, workout_heart_rate, routes, raw_ingest, auth_log; `ensure_partitions()` (current + next month, advisory-lock, idempotent). Paired tests per migration (harness rule). Acceptance: schema-state tests green on container; partition created on demand.
- **slice-2-normalize** (`dev`, deps: slice-1-e2e) — pure normalizer `src/health_db/normalize.py` (no I/O, no DB): canonical unit map (predecessor constants.py parity — read-only reference `../predecessor/src/health_pipeline/constants.py`), non-canonical-unit skip (iron law 4: only kJ→kcal ÷ 4.184 conversion), heart_rate → qty=Avg, blood_pressure → systolic/diastolic split rows, sleep_analysis aggregated → asleep-preference else core+deep+rem, unaggregated sleep stored as-is, display_name mapping, day_kst fixed UTC+9h. Acceptance: ≥95% unit coverage of normalizer/unit policy/day logic (harness); every documented shape tested.
- **slice-2-ingest-pipeline** (`dev`, deps: schema, normalize) — `src/health_db/pipeline.py` + wiring into `/api/ingest`: same tx → dim lookup/autoregister unknown metrics → executemany upsert raw_samples (latest-wins ON CONFLICT) → day-scoped DELETE+INSERT rollup recompute (predecessor ingester.py:237-253 parity) → raw_ingest row (sha256, automation/session headers, outcome); workouts upsert by UUID + workout_heart_rate + routes (kJ→kcal); DB unavailable → 503 + `Retry-After: 300`, payload already disk-archived (iron law 2), marked pending (never raw_ingest 'stored'); `api` acceptance fixture (post_valid_metrics/post_metrics/post_metric_raw/post_valid_workout/db_down). Acceptance: TestIngestPersistsAndRollsUp + TestDbOutageSafety green on container.
- **slice-2-replay** (`dev`, deps: ingest-pipeline) — `health-replay` entry point + `src/health_db/replay.py`: scan archive dir → re-run pipeline per payload (sha256 dedup via raw_ingest → outcome 'replayed' idempotent); pending-queue reprocessing when DB returns; `replay_dir(path)`, `replay_pending()` fixture surface; `archive` fixture (pending_count, write_payload). Acceptance: TestBackfillReplayIdempotent + replay-after-outage green; replay twice = zero row changes.
- **slice-2-mcp-server** (`dev`, deps: ingest-pipeline) — `mcp>=2,<3` dep; `src/health_db/queries.py` (shared query layer: catalog, day summary, series, workouts, detail w/ HR+route, trends, freshness, raw page) + `src/health_db/mcp_server.py`: streamable-http at `/mcp` (mounted ASGI or separate port — per research/mcp-python-sdk.md, fallback separate port OK), Bearer auth middleware (constant-time, shared INGEST_SECRET, 401), 8 tools + 3 resources, structured JSON, pagination (limit default 1000 max 10000), actionable unknown-metric errors; `mcp` fixture using SDK `Client`. Acceptance: TestMcpSurface green via real streamable-http round-trips.
- **slice-2-cli** (`dev`, deps: mcp-server) — `health` entry point (`src/health_db/cli.py`): query-day, metrics, workouts, trends, freshness, raw, routes — thin wrapper over queries.py; JSON to stdout, diagnostics to stderr; no business logic. Acceptance: unit tests with fake query layer; `uv run health --help` works.
- **slice-2-deploy** (`dev`, deps: replay, cli) — Dockerfile (single service image, uv, non-root), docker-compose for server (existing postgres:18 container + env from .env) + local-dev compose; `tests/acceptance/slice_2_perf.py` (90-day series query < 1s on container DB, requires_pg); README: deploy steps, backfill runbook (Manual Export per-year chunks, ≤64 MiB per POST, idempotent re-runs), validation-week procedure, MCP client config examples; `.env.example` PSQL_* docs. Acceptance: `docker build` succeeds, compose config validates, perf test green.
- **slice-2-e2e** (`integration_test`, report_only, deps: deploy) — Run additive acceptance suite `uv run pytest tests/acceptance -m "slice1 or slice2"` + full unit suite + `ruff check src/ tests/`; verify perf test passed; report pass/fail per test. Does NOT fix code; blocking findings via tenet_report_blocking_finding.

### Interface contracts (slice 2)

- `db.pool` — `get_pool()` → psycopg_pool.ConnectionPool from PSQL_* env; `db.migrations.apply_all(conn)` idempotent; `db.partitions.ensure(conn)`.
- `normalize` — `metric_rows(parsed) -> Iterable[SampleRow(name, source, ts, qty, unit)]` (skips non-canonical, splits blood_pressure, sleep rules); `workout_rows(parsed) -> (WorkoutRow, hr_rows, route_rows)`; pure — no DB, no fs.
- `pipeline.ingest_payload(pool, parsed, *, sha256, headers) -> Result(metrics, samples, workouts, outcome)`; raises `DbUnavailable` → route maps to 503 + pending; all-or-nothing per payload tx.
- `replay` — `replay_dir(root) -> ReplayReport`, `replay_pending() -> ReplayReport`; sha256-dedup via raw_ingest (never double-apply).
- `queries` — the single read surface shared by MCP tools + CLI + resources; returns plain dicts (JSON-ready).
- `mcp_server` — ASGI app served alongside or separate-port from ingest; tools/resources call `queries` only; auth middleware before any tool dispatch.
- Fixtures (tests/acceptance): `pg` (disposable container), `api` (TestClient posting to app w/ DB pool), `mcp` (SDK Client vs served endpoint, seeded via api), `archive` (temp archive root + pending queue).
- Slice-1 suites remain green without docker (requires_pg tests skip with explicit reason, never silent).

### Slice-2 use-checkpoint (after slice-2-e2e passes)
User replays captured slice-1 corpus into server PG → runs backfill from phone (per-year chunks) → connects MCP client (Claude etc.) via streamable-http + Bearer → observes hourly deltas landing → post-deploy validation begins.
