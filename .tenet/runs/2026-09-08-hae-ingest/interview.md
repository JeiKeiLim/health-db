# Interview: HAE Ingest — server PostgreSQL health data platform

Date: 2026-09-08
Mode: Full
Rounds: 3

## Mode Selection
- Prompt shown: "Which Tenet execution mode should this run use?" — recommended Full: greenfield build of a new service + schema + MCP query surface + deployment; planning conversation already answered most interview categories.
- User response: Confirmed Full.
- Selected mode: full
- Selection basis: explicit_user_choice

## Clarity Score
- Goal: 1.0 (weight 0.4) — replace predecessor with HAE REST → server Postgres platform; 2-slice delivery with quantified acceptance criteria
- Constraints: 1.0 (weight 0.3) — stack, DB target, deployment, auth, granularity, failure semantics, MCP roster all confirmed
- Success criteria: 1.0 (weight 0.3) — quantified: ack <2s, verbatim archive 100%, zero unexplained errors over post-deploy validation, backfill ≤1 day, hourly delta within 5min, rollup query <1s @ 90-day range
- **Total: 1.0 / 0.8 required** (re-scored after Round 3 closed the gaps flagged at 0.79)

## Round 1 — Planning conversation (pre-Tenet, captured)

### Questions Asked (condensed from the session that preceded this run)
1. Use a community ingest server or build our own for PostgreSQL specifically?
   > Build our own thin ingester; reuse patterns from anym001/healthlog, Ladvien/self-sensored, predecessor normalizer. Learn from existing projects, implement ourselves.
2. Should we use the `postgres` superuser account for the app?
   > No — single dedicated account `health_app` suffices. User created `ROLE health_app` + `DATABASE health_app` (owner health_app) on the server instance. PostgreSQL is only reachable inside server LAN / VPN, so a single account is acceptable. The REST API service is the only externally exposed surface.
3. Migrate the existing SQLite (31M rows) to Postgres?
   > No migration. Health Auto Export supports syncing all history; backfill via the app itself. Keep it simple: HealthKit is the source of truth.
4. Where does Postgres run? Network exposure? Repo layout? Schema ambition?
   > 1) Postgres already runs on server (<server-host>:5432, PostgreSQL 18.6, plain image, no TimescaleDB/PostGIS). 2) Port forwarding will be set up later — only the service, never 5432. 3) This repo (health-db) is the new home; predecessor will be retired after cutover. 4) "Best no matter what" — flexibility and reliability prioritized.

### Decisions Made
- Build own thin ingester (Python/FastAPI + psycopg3 + plain SQL, no ORM) — PostgreSQL-native, informed by community projects (healthlog: dedup key `(metric, time, source)`, verbatim raw archive, replay; self-sensored: auth audit, rate limiting)
- Single DB account: `health_app` @ `<server-host>:5432` / database `health_app`; `.env` holds connection info; app never uses `postgres` superuser
- No SQLite migration; one-time full-history backfill via HAE Manual Export (chunked by year/quarter, Batch Requests ON), then delta automations
- Schema ambitions (to be finalized in spec): dimension tables (`metric_dim`, `source_dim`), `raw_samples` range-partitioned by month with latest-wins `ON CONFLICT` upsert on `(metric, source, ts)`, generated KST day column, `daily_rollups` recompute, `workouts` / `workout_heart_rate` / `routes`, `raw_ingest` verbatim archive with content hash, versioned SQL migrations
- Deployment: single service image on Docker connecting to the existing postgres container; local development uses a local postgres docker container
- Stack continuity: Python, uv, pytest, ruff (same discipline as predecessor)

### Remaining Ambiguities
- Exact MCP tool/resource roster and MCP-native design details (spec phase)
- Analysis features (stress, training load, etc.) — explicitly deferred, out of scope
- TLS/port-forwarding and backup hardening — deferred to final phase

## Round 2 — Tenet interview questions

### Questions Asked
1. How granular should the HAE metric sync be?
   > Minutes for Heart Rate + Step Count (separate automation), hours for everything else. Enables intraday analysis later; dedup keeps it safe.
2. What's the read/query surface for this system?
   > MCP HTTP server (primary) — usable with MCP HTTP clients. Plus a thin CLI wrapper layer for intermediate development testing.
3. How should this deploy on the server?
   > Option 1: service image only, connecting to existing postgres at <server-host>:5432. Local dev uses a postgres docker container locally.
4. Delivery mode (autonomous vs agile)?
   > Agile, exactly 2 slices. Slice 1: REST API ingest server WITHOUT handing data to the database — test with the real Auto Export app sending from the phone, verify it works, gather real payload data. Slice 2: use that real data to develop the database part + rest of the system.
5. Model tier for implementation jobs?
   > Local — finer-grained DAG with explicit per-job acceptance criteria.
6. MCP tools + auth?
   > CLI-mirror tool set with token auth (Bearer on MCP endpoint, same secret as ingest). BUT: "we also need to think about if we are fully taking advantage of MCP here. I don't want just migrating CLI to there. I'd like to take full advantage while we are developing from the ground up." → MCP-native design required: rich tool input schemas, URI-addressable resources (e.g. metric catalog, freshness) so clients can ground calls, LLM-friendly output shaping (pagination/truncation), streamable HTTP transport per current MCP spec.

### Decisions Made
- HAE automations (3): ① Health Metrics (hours, all metrics), ② Health Metrics (minutes, Heart Rate + Step Count only, disjoint set), ③ Workouts (JSON v2, workout metrics ON minutes, route data ON) — per healthlog's split-automation pattern
- Query surface: MCP streamable-HTTP server with tools + resources, token auth (single shared secret; `X-Ingest-Token` header for HAE ingest, Bearer for MCP); design MCP-native, not a CLI port; thin CLI wrapper for dev/testing only
- Delivery: agile mode, 2 slices —
  - **Slice 1 (ingest validation)**: REST ingest service with raw payload capture (verbatim archive to disk), token auth, validation/reporting of received payloads; user tests with the real HAE app from iPhone; acceptance = real payloads accepted, archived, and inspectable; captured payloads become replay fixtures
  - **Slice 2 (database + query)**: PG schema + migrations, ingest pipeline (normalize → upsert → rollups), replay of slice-1 captured payloads + full-history backfill, MCP HTTP query server, CLI wrapper, deployment to server
- Model tier: local

### Remaining Ambiguities
- Final MCP tool/resource names & schemas → resolved in spec phase (research: MCP streamable HTTP spec, predecessor CLI semantics)
- Slice 1 capture format details (file layout) → spec phase
- Rollup parity validation vs predecessor during overlap period → spec phase

## Round 3 — Clarity-gate follow-ups

### Questions Asked
1. Acceptable quantitative targets for ingest, backfill, and queries?
   > Hobby-grade targets. Slice 1: every accepted payload acknowledged <2s, 100% of payloads archived verbatim, zero unexplained 4xx/5xx during a week of real syncs. Slice 2: full-history backfill (~3yr) completes within a day of chunked manual exports; hourly delta syncs land within 5min of the hour; MCP query responses (rollup-level) <1s for 90-day ranges.
2. Failure modes: malformed payload, unknown metric, bad token, DB outage, duplicate re-posts?
   > Archive-first safety. Malformed → 400 + quarantine archive for inspection; unknown metric → accepted, auto-registered (healthlog pattern); auth failure → 401 + audit log (IP, UA); DB outage → 503 with Retry-After, payload still archived to disk so nothing is lost; duplicates → idempotent 200 (content-hash skip or latest-wins upsert).
3. MCP tool/resource roster?
   > Full data surface. Tools: query_day, get_metric_series, list_metrics, list_workouts, get_workout_detail, get_trends, freshness_check, raw_samples (paginated). Resources: health://metrics (catalog with units & coverage), health://freshness, health://schema. Catalog-first, pagination everywhere, summaries by default — LLM grounding as first-class design goal.

### Decisions Made
- Quantified targets: ack <2s; verbatim archive 100%; zero unexplained 4xx/5xx over a post-deploy validation; backfill ≤1 day; hourly delta within 5min of hour; rollup queries <1s for 90-day ranges
- Failure semantics per mode as above (archive-first: nothing is ever lost, everything inspectable)
- MCP roster fixed (8 tools + 3 resources), MCP-native design principles recorded

### Remaining Ambiguities
- None blocking spec; final tool JSON schemas to be authored in spec phase with MCP streamable-HTTP research notes

## Delivery Mode Decision
- Prompt shown: standalone question "How should the run be delivered? (autonomous = one end-to-end run after a single confirmation; agile = sliced with checkpoints between slices)"
- User response: "Agile, but what I'd like to do is for testing ingestion part without handing them to database. ... gather some real data and use them to continue developing the rest part of database thing. so it's just 2 slices agile mode."
- Selected delivery_mode: agile
- Selection basis: explicit_user_choice

## Model Tier Decision
- Prompt shown: "Which tier of model will execute the implementation jobs?" (frontier vs local, with decomposition implications)
- User response: "Local"
- Selected model_tier: local
- Selection basis: explicit_user_choice

## Summary
Replace the predecessor (HAE AutoSync → SQLite) pipeline with a ground-up server platform: Health Auto Export REST automations POST JSON v2 to a FastAPI ingest service on a self-hosted Docker host, storing into the existing PostgreSQL 18.6 instance (single `health_app` role/db at <server-host>:5432) with a partitioned, dimension-modeled schema; no SQLite migration — history comes from HAE full-history sync. Queries served by an MCP-native streamable-HTTP server (tools + resources, token auth) with a thin CLI dev wrapper. 3 HAE automations (hours-all / minutes-HR+Steps / workouts). Delivered in 2 agile slices: ① real-app ingest validation with raw payload capture, ② database + MCP + backfill + deployment. Executed by local-tier models via a fine-grained DAG. This repo (health-db) is the new home; predecessor retires after cutover.