# Scenarios: 2026-09-08-hae-ingest

## Scenarios (Success)

1. **Hourly delta sync**: HAE Metrics-all automation fires at 10:00 KST; service authenticates, archives to `data/archive/2026/09/08/`, parses JSON v2, and returns 200 `{"outcome":"stored","metrics":42,"samples":1034}` within 2s. Slice 2: rows upsert into `raw_samples`, affected `day_kst` rollups recomputed in the same transaction; `raw_ingest` records sha256 + headers. Next hour's overlapping re-send of "Standard" range updates (not duplicates) corrected sleep values — latest-wins, counts unchanged.

2. **Real-app validation (slice 1 checkpoint)**: User configures 3 automations pointing at dev machine over LAN Wi-Fi, triggers a Manual Export from the phone; payload lands archived verbatim; `GET /api/ingest/stats` shows stored count increment; curl with bad token returns 401 and stats/auth_log show the attempt. User confirms phone-side "success" in HAE activity logs for a full validation day.

3. **Full-history backfill**: User runs Manual Export in per-year chunks over an evening; each chunk POSTs (Batch Requests ON → multiple sequential requests); all chunks land within one day; re-running the 2024 chunk changes nothing (row counts identical before/after — proven idempotency); rollups for all backfilled days exist and match Python-recomputed expectations from the same payloads.

4. **MCP agent query session**: An MCP HTTP client (Bearer auth) lists tools, reads `health://metrics` catalog to ground itself, calls `get_metric_series("step_count", 90 days)` — response < 1s, structured JSON with pagination metadata; then `freshness_check()` reports per-metric latest days with median-based staleness.

5. **DB outage resilience (slice 2)**: Postgres container briefly stopped; HAE sync arrives; service archives payload to disk, returns 503 + Retry-After; when DB returns, replay tool (or auto-resume) processes archived-but-unstored payloads; no data lost; stats show replayed count.

6. **Workout with route + HR series**: Workouts automation delivers a JSON v2 workout with heartRateData and route; workout row upserts by UUID, 45 min HR samples + 1,200 route points land in their tables; `get_workout_detail` MCP tool returns the workout with nested HR and route arrays ordered by timestamp.

7. **Unknown metric auto-registration**: HAE adds a new metric (e.g. iOS update); payload accepted; metric auto-registered with pass-through unit; next sync's samples upsert cleanly; `list_metrics` shows it with coverage.

## Anti-Scenarios (Failure)

1. **Double-count on overlapping re-sends**: Must NEVER happen — overlapping date ranges with corrected values must update rows, not append. Prevented by latest-wins ON CONFLICT + sha256 duplicate skip; tested by double-posting identical and overlapping payloads and asserting equal row counts.

2. **Lost payload on processing failure**: Must NEVER happen — malformed JSON or DB outage must always leave the verbatim payload on disk (quarantine or pending-replay). A payload that returns non-2xx without being archived is a bug.

3. **Non-canonical unit pollution**: Must NEVER store weight-in-lb next to weight-in-kg rows (the SQLite 57M-row unit-pollution incident). Non-canonical samples skipped at normalize; tests cover lb/st/degF/mi variants being skipped while kg/degC/km pass.

4. **Timezone drift in day boundaries**: Must NEVER compute rollup days with server-local UTC or a DST-observing zone — a KST day must equal fixed UTC+9 day. Generated column enforces; tests cover samples around midnight KST boundary (e.g. 23:59 vs 00:01) landing in different days.

5. **Secret leakage**: Logs/audit/errors must never contain INGEST_SECRET or PSQL_PASSWORD. auth_log stores IP/UA/reason only. Tests assert redaction.

6. **Unauthenticated MCP access**: Any MCP call without valid Bearer → 401; no tool enumeration or data exposure pre-auth. Tests cover all endpoints.

7. **Silent metric-name collision regression**: snake_case REST names must never multiply into duplicate metrics (the predecessor `active_energy` vs `Active Energy` double-count). Single-lane + metric_dim UNIQUE(name) prevents; test asserts one metric row for repeated payloads.

8. **Test runs against production server DB**: Must NEVER connect integration tests to <server-host>:5432; tests are container-only, enforced by test DB URL convention + harness iron law.