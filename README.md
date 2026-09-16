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
uv run health raw --metric step_count --from 2026-09-01 --to 2026-09-10 --limit 1000
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
  canonical rows). The only conversion is kJ→kcal ÷ 4.184. **All workout energies are
  stored in kcal** (`active_energy_kcal`, `total_energy`, `basal_energy`): real HAE v2
  payloads send `activeEnergyBurned`/`totalEnergy` as `{qty, units:'kJ'}` objects and
  `activeEnergy`/`basalEnergy` as per-minute `kJ` point lists, which are summed then
  converted. Distance (`distance_km`, `walking_running_distance_km`), HR, cadence,
  speed, temperature, humidity and intensity are read from the real
  `{qty, units}`/point-list shapes as well as the simplified fixture shapes.
- **KST days**: `day_kst` is a stored generated column with fixed UTC+9 — rollup
  boundaries are KST calendar days, never server-local time.
- **Rollups**: `daily_rollups` are recomputed per affected day inside the ingest
  transaction; queries read rollups (90-day series < 1 s).

## Deployment (Docker server)

Target: Docker **Docker** (the host OS, x86 models only — ARM units like the
ARM model cannot run Docker). Prerequisites: an existing postgres:18 container on
the hostwith db/role `health_app` created and its port published on the host (e.g. 5432).

The image is **built on the hostover SSH** (`sudo docker build`) — Docker and
the shell share one Docker daemon, so the image lands in the Image list and the Project
can use it. Docker's own Image Import only accepts built image tarballs, and
its Projects don't compile a Dockerfile, so those aren't needed for this flow.

1. **Get the repo onto the host** at `health-db` (git clone over SSH, or
   upload the working tree with file manager).

2. **Prepare `.env`** (gitignored — never commit) *next to `docker-compose.yml` in that
   folder*. Docker reads it under that exact dotted name (enable hidden files
   in file manager if you can't see it):
   ```
   PSQL_USER=health_app
   PSQL_TABLE=health_app
   PSQL_PASSWORD=<postgres password>
   PSQL_URL=< LAN IP or hostname>       # e.g. 203.0.113.10 — NOT a container name
   PSQL_PORT=5432                          # the postgres container's published host port
   INGEST_SECRET=$(openssl rand -hex 32)
   INGEST_HEADER=X-Ingest-Token
   MAX_PAYLOAD_BYTES=67108864
   ```
   See `.env.example` for the full key reference (`PSQL_URL` is the **host**;
   `PSQL_TABLE` is the **database name** — HAE's odd key name is preserved for parity).
   Keep `env_file` in the simple list form in `docker-compose.yml`: Docker
   ships Compose v2.20, which rejects the extended `path:`/`required:` syntax.

   **How env vars reach the app** (they are never baked into the image — image layers
   are readable by anyone with the image): the image declares every key with an empty
   default so Docker's create-container wizard lists them and `docker inspect`
   documents them; the real values are injected at container start, either from `.env`
   via the compose `env_file` (the Project path above) or typed into the wizard's
   Environment tab (standalone-container path). Both override the image defaults. Leaving
   `INGEST_SECRET`/`PSQL_*` blank keeps the app fail-closed: every ingest/MCP request is
   401'd until a real secret is supplied, and the health probe never dials ambient
   `localhost:5432`.

3. **Create the archive folder** (Docker never creates bind-mount folders, and
   the image runs as UID 10001, not your the host OS user):
   ```sh
   sudo mkdir -p health-db/data
   sudo chown -R 10001:10001 health-db/data
   ```

4. **Build the image on the host** (SSH in first; this can take a while on hostCPUs):
   ```sh
   cd health-db
   sudo docker build -t health-db:latest .
   ```
   `.dockerignore` keeps `.git`, `.env`, `data/`, `.tenet/` etc. out of the build context,
   so no health data or secrets end up in the image. Verify the image appears in
   Docker → **Image**.

5. **Create the Project**: Docker → **Project → Create** → name `health-db`,
   path `health-db` (the wizard picks up `docker-compose.yml`, which
   references `health-db:latest`). The entrypoint runs migrations at startup
   (`health-migrate` → `migrations/0001_init.sql`), then serves ingest on :8000 and MCP
   on :8100. The verbatim archive lives in `health-db/data` and survives
   container recreation (and shows up in host backups, unlike a named volume in
   `var/lib/docker`).

   To ship code changes: `git pull`, rebuild with step 4, then Project → **Build**
   (don't delete the project). Watch startup in Container → health-db → Details → Log.

   *Alternative — standalone container instead of a Project:* Docker →
   **Container → Create** from `health-db:latest`; the Environment tab lists every key
   from the image declaration (step 2's layering) so you can type the values there.
   Map 8000/8100, mount the same data folder, and add `restart: unless-stopped` via the
   settings. Note the host OS won't let you edit a container's env after creation (duplicate-only),
   which is why the Project + `.env` path is the documented default.

6. **Verify**:
   ```sh
   curl http://<server>:8000/api/health                      # {"status":"ok",...}
   curl -H "X-Ingest-Token: $INGEST_SECRET" http://<server>:8000/api/ingest/stats
   ```

Shell alternative (same build, runs the stack without Docker Project tracking):
```sh
cd health-db
sudo docker compose up -d --build
```

Local dev variant: `docker compose -f docker-compose.local.yml up --build` — spins up a
disposable postgres:18 plus the service, building the image from source.

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

### Repairing rows written by an older normalizer

`health-replay --dir` short-circuits on `raw_ingest.sha256` (duplicate), so it cannot
re-normalize payloads already in the DB. To repair rows written by a buggy/older
normalizer, run the force/repair path against the archive on disk:

```sh
uv run health-replay --dir data/archive --force     # --repair is an alias
```

This re-normalizes every archived payload even when its sha already exists and
re-upserts with latest-wins semantics, so **workout/HR/route columns are rewritten
with corrected values while `raw_samples` and `daily_rollups` are unchanged**. It is
idempotent — running it twice changes nothing the second time — and never deletes
`raw_ingest` rows (the audit row is updated in place with `outcome='repaired'`). It
reads only from the on-disk archive, so it is safe on the host.

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

**When clients reach MCP by a name other than localhost** ( hosthostname, or a
Docker reverse-proxy host:port), set `MCP_ALLOWED_HOSTS` in `.env` — a
comma-separated list of Host values, e.g. `healthbox.example:8443,healthbox.example`. The MCP
transport validates the `Host` header (DNS-rebinding protection) against a
localhost-only default allowlist; an unlisted name is rejected with
`421 Invalid Host header`. `base:*` wildcards are supported (any port on
`base`). The Bearer check remains the access control — this list only decides
which hostnames may reach it.

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
| `PSQL_URL` | postgres **host** ( LAN IP/hostname, or `postgres` service name in local compose) | `localhost` |
| `PSQL_PORT` | postgres port | `5432` |
| `PSQL_USER` | role name | `health_app` |
| `PSQL_TABLE` | database **name** (HAE parity naming) | `health_app` |
| `PSQL_PASSWORD` | role password | — |
| `INGEST_SECRET` | shared secret (ingest header + MCP Bearer) | — |
| `INGEST_HEADER` | ingest header name | `X-Ingest-Token` |
| `MAX_PAYLOAD_BYTES` | 413 threshold | `67108864` (64 MiB) |
| `HOST` / `PORT` | ingest bind | `0.0.0.0:8000` |
| `MCP_HOST` / `MCP_PORT` | MCP bind | `0.0.0.0:8100` |
| `MCP_ALLOWED_HOSTS` | extra Host-header names for MCP (reverse proxy/server), comma-separated | localhost only |

Out of scope (later manual steps): TLS termination / port forwarding behind the Docker
reverse proxy; analysis features; predecessor decommission.