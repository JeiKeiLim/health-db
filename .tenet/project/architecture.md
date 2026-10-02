# Project Architecture

## Tech Stack

- **Language**: Python (uv-managed, `pyproject.toml`), same discipline as predecessor
- **Ingest service**: FastAPI (ASGI, uvicorn) — one HTTP app hosting both the ingest endpoint and the MCP streamable-HTTP server
- **Database driver**: psycopg3, plain SQL, no ORM
- **Database**: PostgreSQL 18.6 on self-hosted server (`<server-host>:5432`), database `health_app`, role `health_app` (single dedicated account; `postgres` superuser never used by the app). Plain image — no TimescaleDB/PostGIS available; use native PG 18 features only.
- **MCP transport**: streamable HTTP per current MCP specification, Bearer token auth
- **Testing**: pytest; ruff for lint
- **Deployment**: Docker image on Docker, connecting to the existing postgres container over the server network. Local development runs a local postgres Docker container.
- **HAE client config**: 3 REST automations (metrics-hours / metrics-minutes-HR+Steps / workouts), JSON format, Export Version 2, token header auth

## Data Flow

```
HAE iPhone (3 automations) --POST /api/ingest (token)--> FastAPI service (server Docker)
   ├─ auth check (401 + audit log on failure)
   ├─ verbatim raw payload archive to disk (before anything else; content-hash skip)
   ├─ parse JSON v2 → normalize (canonical units, KST day boundaries)
   └─ idempotent upsert into PostgreSQL (latest-wins on conflict) → recompute daily_rollups

MCP clients --streamable HTTP (Bearer)--> same FastAPI app → tools/resources → Postgres

Backfill: HAE Manual Export (chunked ~yearly, Batch Requests ON) → same ingest path
```

## Persistence Design (interview-agreed baseline; final DDL owned by spec)

- `metric_dim`, `source_dim` — smallint-FK dimension tables (canonical units centralized)
- `raw_samples` — natively range-partitioned by month; key `(metric, source, ts)` with latest-wins `ON CONFLICT DO UPDATE`; generated KST day column
- `daily_rollups` — real table, transactionally recomputed per affected metric/day (KST boundaries)
- `workouts`, `workout_heart_rate`, `routes` — plain lat/lon (no PostGIS)
- `raw_ingest` — verbatim jsonb archive + content hash (replay/audit); disk archive mirrors this before DB write
- Versioned SQL migrations from day one

## Integration Contracts

- **HAE ingest contract**: POST JSON v2 payload (`data.metrics[]`, `data.workouts[]`, ...), `X-Ingest-Token` header (name configurable), auto-included headers (automation-name/id/aggregation/period, session-id) recorded for audit
- **MCP contract**: streamable HTTP, Bearer auth, 8 tools + 3 resources (roster fixed in interview; JSON schemas authored in spec)
- **Environment**: `.env` at repo root holds connection info (PSQL_USER/PSQL_PASSWORD/PSQL_URL/PSQL_PORT); app-level config reads from environment, never hardcodes credentials

## Operational Notes

- PostgreSQL reachable only inside server LAN / VPN; port-forwarding (443 → reverse proxy → service) is a later hardening step, along with pg_dump backup cron
- Rollup parity vs predecessor SQLite during the overlap period validates the cutover