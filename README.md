# health-db

Self-hosted Apple Health data platform: the **Health Auto Export (HAE)** iOS app POSTs
JSON v2 exports to a FastAPI ingest service, which archives every payload verbatim
(archive-first), normalizes it, and stores it in PostgreSQL. Queries are served by an
MCP streamable-HTTP server (8 tools + 3 resources, Bearer auth) with a thin CLI wrapper
for dev testing. Runs as a single Docker container on a Docker hostnext to an
existing postgres:18 container.

```
iPhone (HAE automations) ──POST /api/ingest──►  ingest :8000 ──► data/archive/ (verbatim, iron law)
                                                    │                     │
                                                    ▼                     ▼
                                             PostgreSQL 18 ◄── health-replay (backfill / outage queue)
                                                    │
MCP clients (Claude, …) ──streamable-http :8100/mcp──► queries.py (shared read layer) ◄── health CLI
```

## Surfaces

| Surface | Endpoint | Auth |
|---|---|---|
| Ingest | `POST /api/ingest` | `X-Ingest-Token` header (`INGEST_SECRET`) |
| Liveness | `GET /api/health` | none (includes DB connectivity) |
| Ingest stats | `GET /api/ingest/stats` | `INGEST_SECRET` |
| MCP | `http://<host>:8100/mcp` (streamable-http) | `Authorization: Bearer <INGEST_SECRET>` |
| CLI | `uv run health <command>` | uses `PSQL_*` env directly |

### MCP tools
`query_day(date, metric?)` · `get_metric_series(metric, from, to)` · `list_metrics()` ·
`list_workouts(from, to)` · `get_workout_detail(id)` · `get_trends(metric, window)` ·
`freshness_check()` · `raw_samples(metric?, from?, to?, limit, offset)`

### MCP resources
`health://metrics` (catalog) · `health://freshness` · `health://schema`

### CLI
```
uv run health query-day 2026-09-10
uv run health metrics
uv run health workouts --from 2026-09-01 --to 2026-09-10
uv run health trends step_count --window 30
uv run health freshness
uv run health raw step_count --from 2026-09-01 --to 2026-09-10 --limit 1000
uv run health routes <workout-id>
```
JSON to stdout, diagnostics to stderr. Thin wrapper over the same query layer the MCP
tools use.

## Data semantics (the invariants)

- **Archive-first**: every authorized payload is on disk (`data/archive/YYYY/MM/DD/`)
  before any processing verdict. DB outages queue payloads in `data/pending/` (503 +
  `Retry-After: 300`) and `health-replay --pending` drains them when the DB returns.
  Nothing is ever lost.
- **Idempotency**: duplicate payloads (same sha256) return `200 {"outcome":"duplicate"}`;
  samples upsert latest-wins on `(metric_id, source_id, ts)`; workouts upsert by HAE
  UUID. Re-posting any chunk is always safe — row counts never change.
- **Canonical units**: non-canonical-unit samples are skipped (never stored next to
  canonical rows). The only conversion is kJ→kcal ÷ 4.184.
- **KST days**: `day_kst` is a stored generated column with fixed UTC+9 — rollup
  boundaries are KST calendar days, never server-local time.
- **Rollups**: `daily_rollups` are recomputed per affected day inside the ingest
  transaction; queries read rollups (90-day series < 1 s).

## Deployment (Docker server)

Prerequisites: an existing postgres:18 container on the host, on a Docker network
(referred to below as `health-net`), with db/role `health_app` created.

1. **Prepare `.env`** (gitignored — never commit):
   ```
   PSQL_USER=health_app
   PSQL_TABLE=health_app
   PSQL_PASSWORD=<postgres password>
   PSQL_URL=<postgres container name on the shared network>   # e.g. postgres, not an external hostname
   PSQL_PORT=5432
   INGEST_SECRET=$(openssl rand -hex 32)
   INGEST_HEADER=X-Ingest-Token
   MAX_PAYLOAD_BYTES=67108864
   ```
   See `.env.example` for the full key reference (`PSQL_URL` is the **host**;
   `PSQL_TABLE` is the **database name** — HAE's odd key name is preserved for parity).

2. **Build and run** on the host(Docker → Project, or shell):
   ```sh
   docker build -t health-db:latest .
   docker compose -f docker-compose.server.yml up -d
   ```
   The compose file expects `health-net` as an **external** network (edit to match your
   postgres container's network). The container runs migrations at startup
   (`health-migrate` → `migrations/0001_init.sql`), then serves ingest on :8000 and MCP
   on :8100. The verbatim archive lives in the `health-data` volume and survives
   container recreation.

3. **Verify**:
   ```sh
   curl http://<server>:8000/api/health                      # {"status":"ok",...}
   curl -H "X-Ingest-Token: $INGEST_SECRET" http://<server>:8000/api/ingest/stats
   ```

Local dev variant: `docker compose -f docker-compose.local.yml up --build` — spins up a
disposable postgres:18 plus the service with the same image.

## HAE app configuration (iPhone)

Three REST automations (JSON, **v2** format, **Batch Requests ON**, header
`X-Ingest-Token: <INGEST_SECRET>`, timeout 60s), pointed at
`http://<dev-machine-or-server>:8000/api/ingest`:

| # | Name | Metrics | Grouping | Cadence |
|---|---|---|---|---|
| 1 | Metrics-all | all metrics, Summarize ON | hours | hourly |
| 2 | Metrics-HR+Steps | Heart Rate + Step Count only | minutes | hourly |
| 3 | Workouts | Include Workout Metrics ON (minutes) + Include Route Data ON | — | daily |

Automations 1 and 2 are deliberately disjoint to keep hourly payloads small.

## Backfill runbook (full history)

History is backfilled through the same ingest endpoint — no SQLite migration.

1. In HAE: **Manual Export** → JSON v2, per-year date-range chunks (one chunk per year
   of history), all metrics + workouts, Summarize ON, Batch Requests ON.
   Each POST is ≤ 64 MiB (`MAX_PAYLOAD_BYTES`); Batch Requests splits larger exports
   into sequential requests automatically.
2. Chunks flow through the same `/api/ingest` pipeline as live syncs: archived →
   normalized → upserted → rollups recomputed.
3. **Re-runs are free**: re-posting any chunk (or re-running it via
   `health-replay --dir data/archive`) changes nothing — sha256 dedup + latest-wins
   upserts make overlap safe. Verify by re-running a chunk and comparing row counts
   (spec scenario 3).
4. If the DB was down during a chunk, the payload sits in `data/pending/` (service
   answered 503); drain with `docker exec health-db health-replay --pending`.

## Post-deploy validation (post-deploy procedure)

1. Day 0: backfill completes (above); `GET /api/ingest/stats` shows
   `stored` ≈ chunk count, `quarantined: 0`, `unauthorized_attempts: 0`.
2. Days 1–7: leave the 3 automations running. Hourly deltas should land within
   5 minutes of the hour — check `stats.last_received_at` / `last_automation` and
   `raw_ingest.received_at` (spec success criterion 3).
3. Spot-check parity: for a few captured payloads, `daily_rollups` values must match
   the payload's own aggregates (recompute from the archived JSON if in doubt).
4. Zero unexplained 4xx/5xx: every non-2xx in the week must be accounted for
   (quarantine / 413 oversize / deliberate 401 probes). Anything else is a bug.
5. Query checks: `freshness` shows current-day coverage per metric; a 90-day
   `get_metric_series` answers < 1 s (perf contract, `tests/acceptance/slice_2_perf.py`).

## MCP client configuration

MCP endpoint: `http://<server>:8100/mcp` (streamable-http), auth
`Authorization: Bearer <INGEST_SECRET>` (same secret as ingest).

Example client config (Claude Desktop / any streamable-http MCP client):

```json
{
  "mcpServers": {
    "health-db": {
      "type": "http",
      "url": "http://<docker-host>:8100/mcp",
      "headers": {
        "Authorization": "Bearer <INGEST_SECRET>"
      }
    }
  }
}
```

Recommended agent flow: read `health://metrics` first to ground on the catalog, then
`get_metric_series` / `get_trends` for rollup-level questions, `raw_samples` for
fine-grained data (limit default 1000, max 10000), `freshness_check()` before
interpreting recent days.

## Testing

```sh
uv run pytest                       # unit + acceptance (pg tests need docker)
uv run pytest -m requires_pg        # container-backed acceptance only
uv run pytest tests/acceptance/slice_2_perf.py -m requires_pg   # perf contract (<1s @ 90d)
uv run ruff check src/ tests/
```

Integration tests always run against disposable postgres:18 containers — never the host
database. Slice-1 captured real payloads in `data/archive/` are the canonical fixtures
(never committed).

## Environment reference

| Key | Meaning | Default |
|---|---|---|
| `PSQL_URL` | postgres **host** (container name on the hostnetwork) | `localhost` |
| `PSQL_PORT` | postgres port | `5432` |
| `PSQL_USER` | role name | `health_app` |
| `PSQL_TABLE` | database **name** (HAE parity naming) | `health_app` |
| `PSQL_PASSWORD` | role password | — |
| `INGEST_SECRET` | shared secret (ingest header + MCP Bearer) | — |
| `INGEST_HEADER` | ingest header name | `X-Ingest-Token` |
| `MAX_PAYLOAD_BYTES` | 413 threshold | `67108864` (64 MiB) |
| `HOST` / `PORT` | ingest bind | `0.0.0.0:8000` |
| `MCP_HOST` / `MCP_PORT` | MCP bind | `0.0.0.0:8100` |

Out of scope (later manual steps): TLS termination / port forwarding behind the Docker
reverse proxy; analysis features; predecessor decommission.