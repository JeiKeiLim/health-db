---
delivery_mode: autonomous
---

# Spec: MCP analysis parity — sleep-session detail, route paging, freshness activity day

Source: consumer spec `specs/2026-09-28-mcp-analysis-parity.md` (written by
Max, the Hermes health agent — primary MCP consumer, after a live audit of
the MCP surface at repo HEAD `dd668f8`), adopted via interview
`.tenet/runs/2026-09-28-mcp-analysis-parity/interview.md` (clarity 0.835).
This document is the executable crystallization; where terse, the consumer
spec's exact values govern.

## Purpose

Close the last MCP capability gaps that still force the consumer to fall
back to the legacy `predecessor` pipeline: sleep-session detail (stages
+ real bed/wake windows), workout route paging, activity-only freshness
marker, workout-list pagination, and probe-metric cleanup — so the legacy
pipeline can be retired.

## Tech Stack

Unchanged project doctrine (.tenet/project/architecture.md): Python 3.12+
(uv), psycopg3 (plain SQL, no ORM), FastAPI, mcp SDK v2, PostgreSQL 18,
pytest + ruff, Docker. **No new
dependencies.** One additive migration (`0003_sleep_sessions.sql`); no
ALTER/DROP on existing tables (0001/0002 frozen — additive-only rule from
run 2026-09-17-normalizer-audit).

## Verified context

- Consumer live audit passed parity spot-checks (9/27: sleep 7.6521 h,
  steps 10,472.83, RHR 57, active energy 716.4 kcal, HRV 521.44).
- Orchestrator verified every "current behavior" claim at HEAD `dd668f8`:
  normalizer sleep branch drops the session fields (src/health_db/normalize.py:635-676),
  get_workout_detail inlines all route points (src/health_db/queries.py:550-558),
  list_workouts hardcodes LIMIT 1000 (src/health_db/queries.py:484),
  freshness has latest_complete_day only (src/health_db/queries.py:189+).
- Archive corpus: 2,500 backfill sleep points (100% with sleepStart/End;
  1,200 staged; 1,190 awake>0) + 130 recent-daily points, census-locked in
  tests/unit/test_census_lock.py against tests/fixtures/parse_everything/sleep_shape_matrix.json.

## Changes

### 1. `sleep_sessions` session lane (P1 — cutover blocker)

Same architectural pattern as `ecgs`/`heart_rate_notifications` (migration
0002 + pipeline `_upsert_ecg_lane` src/health_db/pipeline.py:239 + query
layer + MCP tool + CLI + schema resource). OUT of `daily_rollups`.

- **One row per recognized sleep record** — every payload point that today
  yields a `sleep_analysis` or `sleep_in_bed` row (Watch nights, AutoSleep
  naps/dozes, in-bed-era rows). Per-source rows are NEVER merged; nap/doze
  segments stay distinct; NO nap filtering (consumer applies its own
  16:00–18:59 exclusion client-side).
- **Recognition**: identical shape guard as the existing aggregated-sleep
  branch — `date` + ∩ {totalSleep, asleep, core, deep, rem, inBed,
  sleepStart} ≠ ∅ (src/health_db/normalize.py `_SLEEP_FIELD_KEYS`). Points
  matching earlier-precedence shapes ({qty, date}, {startDate, endDate,
  qty}) are NOT session records and yield no session row.
- **Fields verbatim from payload** (durations hours): `source_name`,
  `record_date` (payload `date`), `day_kst` (generated, fixed UTC+9 — same
  expression family as raw_samples; bucketing identical to sleep_analysis
  rows), `sleep_start`, `sleep_end`, `in_bed_start`, `in_bed_end`,
  `total_sleep`, `asleep`, `deep`, `core`, `rem`, `awake`, `in_bed`.
- **Semantics**: `asleep: 0` on Watch rows is the known sentinel — stored
  verbatim, never re-derived. Mixed-stage rows (49 in backfill) carry a
  real `asleep` — stored verbatim. Rows lacking parseable `sleep_start`
  are skipped AND counted under new additive key `sleep_session` in the
  ingest response `skipped` object (zero-silent-drops; added to
  `SKIPPED_KEYS` src/health_db/payloads.py:82). Missing `sleep_end` /
  in-bed fields stay NULL.
- **Upsert**: latest-wins ON CONFLICT `(source_name, sleep_start)` DO
  UPDATE; re-ingest fully idempotent. Single-writer pipeline transaction
  (ingest_payload) — no parallel-write semantics needed.
- **Existing `sleep_analysis`/`sleep_in_bed` emissions NOT touched**;
  `tests/unit/test_census_lock.py` passes unchanged; `sleep_shape_matrix.json` FROZEN.
- **MCP tool** `list_sleep_sessions(from?, to?, limit=100, offset=0)` — newest
  first (sleep_start DESC), KST-day range filter on `day_kst`, `limit`
  clamped `[1, 500]`, `offset` ≥ 0 (validated like `_validated_offset`),
  rows `{id, source, day, sleep_start, sleep_end, in_bed_start, in_bed_end,
  total_sleep, asleep, deep, core, rem, awake, in_bed}`, timestamps via
  `_ts_iso`. Response includes `count`, `limit`, `offset`, and
  `next_offset` (`offset + limit` when more rows exist, else `null`) — the
  raw_samples/list_workouts house convention (queries.py:908-967). Empty
  range → `{"sessions": [], "count": 0, ..., "next_offset": null}` (never
  an error); paging past end → empty array + `next_offset: null`.
- **CLI** `health sleep --from YYYY-MM-DD [--to YYYY-MM-DD] [--limit N]
  [--offset N]` — JSON to stdout, diagnostics to stderr (house convention).
- `health://schema` gains the `sleep_sessions` table entry.

**Real payload excerpts (verbatim; must survive ingest exactly):**

Night 2026-09-27→28 (Watch):
```json
{"date": "2026-09-28 00:00:00 +0900", "source": "Jongkuk의 Apple Watch",
 "sleepStart": "2026-09-27 23:05:35 +0900", "sleepEnd": "2026-09-28 05:49:54 +0900",
 "inBedStart": "2026-09-27 23:05:35 +0900", "inBedEnd": "2026-09-28 05:49:54 +0900",
 "totalSleep": 6.2614, "asleep": 0, "inBed": 0, "awake": 0.262,
 "core": 3.7234, "deep": 1.043, "rem": 1.495}
```
Night 2026-09-16→17 (Watch) + AutoSleep nap (two records, same day, never merged):
```json
{"date": "2026-09-17 00:00:00 +0900", "source": "Jongkuk의 Apple Watch",
 "sleepStart": "2026-09-16 22:11:12 +0900", "sleepEnd": "2026-09-17 04:58:30 +0900",
 "inBedStart": "2026-09-16 22:11:12 +0900", "inBedEnd": "2026-09-17 04:58:30 +0900",
 "totalSleep": 6.3746, "asleep": 0, "inBed": 0, "awake": 0.05,
 "core": 3.5891, "deep": 1.0473, "rem": 1.7382}

{"date": "2026-09-17 00:00:00 +0900", "source": "AutoSleep",
 "sleepStart": "2026-09-16 17:35:00 +0900", "sleepEnd": "2026-09-16 18:10:00 +0900",
 "inBedStart": "2026-09-16 17:35:00 +0900", "inBedEnd": "2026-09-16 18:10:00 +0900",
 "totalSleep": 0.5833333333333334, "asleep": 0.5833333333333334, "inBed": 0.5833333333333334,
 "awake": 0, "core": 0, "deep": 0, "rem": 0}
```

### 2. `get_workout_detail` route paging (P2)

- Signature: `get_workout_detail(id, route_offset=0, route_limit=1000)`;
  `route_limit` clamps `[1, 10000]` via existing `_bounded_limit`
  (src/health_db/queries.py).
- Response adds: `route_count` (total points), `route_offset`,
  `route_limit`, `next_route_offset` (`None` on last page) — mirror
  `get_ecg_detail` (queries.py:705) exactly.
- `hr` and `heart_rate_recovery` stay inline unpaged. All other existing
  fields unchanged.
- CLI `health routes <id>` gains the same offset/limit options.

### 3. `freshness_check` — `latest_activity_day` (P2)

Purely additive: add `latest_activity_day` to `freshness_check` AND the
`health://freshness` resource = `max(last_day)` over exactly:
`sleep_analysis`, `heart_rate_variability`, `resting_heart_rate`,
`step_count`, `active_energy`, `apple_exercise_time`,
`walking_running_distance`. Missing metrics ignored; `None` when none
exist. `latest_complete_day` (median consensus) and `stale` semantics
UNCHANGED (nutrition feed legitimately pins the median).

### 4. `list_workouts` pagination (P3)

- Add `limit` (default 500, clamp `[1, 2000]`) and `offset`; response adds
  `limit`, `offset`, `next_offset` (`None` = last page). Keep existing
  `count` field. Replaces hardcoded LIMIT 1000 (no more silent truncation).

### 5. Probe-metric cleanup (P3 — ops only)

Remove `__probe_metric` / `__probe_metric2` from `metric_dim`,
`daily_rollups`, `raw_samples` via the consumer spec's transactional SQL
(§5 of specs/2026-09-28-mcp-analysis-parity.md). Runs AFTER backfill
replay so replay cannot resurrect probes first. Not a migration. Payloads
stay archived (acceptable; purge is a possible follow-up).

## Database Schema

`migrations/0003_sleep_sessions.sql` (additive only, one transaction —
house migration convention; forward-only, no down path; failure aborts
startup with no partial state):

| Table | Column | Type | Constraints |
|---|---|---|---|
| sleep_sessions | id | bigint GENERATED ALWAYS AS IDENTITY | PK |
| sleep_sessions | source_name | text | NOT NULL |
| sleep_sessions | record_date | timestamptz | NOT NULL (payload `date` verbatim) |
| sleep_sessions | day_kst | date | GENERATED ALWAYS — KST day of record_date (fixed +9h expression, same family as 0001 raw_samples.day_kst) |
| sleep_sessions | sleep_start | timestamptz | NOT NULL |
| sleep_sessions | sleep_end | timestamptz | |
| sleep_sessions | in_bed_start | timestamptz | |
| sleep_sessions | in_bed_end | timestamptz | |
| sleep_sessions | total_sleep | double precision | |
| sleep_sessions | asleep | double precision | |
| sleep_sessions | deep | double precision | |
| sleep_sessions | core | double precision | |
| sleep_sessions | rem | double precision | |
| sleep_sessions | awake | double precision | |
| sleep_sessions | in_bed | double precision | |
| sleep_sessions | ingested_at | timestamptz | NOT NULL DEFAULT now() |
| sleep_sessions | | | UNIQUE (source_name, sleep_start) |

Plus: `CREATE INDEX sleep_sessions_day_kst_idx ON sleep_sessions (day_kst);`
No partition DDL, no rollup integration.

## API Surface Delta

| Surface | Change | Auth |
|---|---|---|
| MCP tool `list_sleep_sessions(from?, to?, limit=100, offset=0)` | NEW | Bearer (inherited — no new auth surface, user-confirmed) |
| MCP tool `get_workout_detail(id, route_offset?, route_limit?)` | route paging (additive fields) | Bearer |
| MCP tool `freshness_check()` | + `latest_activity_day` | Bearer |
| MCP tool `list_workouts(from?, to?, limit?, offset?)` | pagination (additive fields) | Bearer |
| Resource `health://schema` | + sleep_sessions table entry | Bearer |
| Resource `health://freshness` | + latest_activity_day | Bearer |
| CLI `health sleep` | NEW (mirrors tool) | PSQL env |
| CLI `health routes <id>` | + offset/limit options | PSQL env |
| Ingest `/api/ingest` | unchanged rows; `skipped` gains additive key `sleep_session` | X-Ingest-Token |

## Auth Flow

Unchanged: MCP Bearer token (`Authorization: Bearer <INGEST_SECRET>`), DNS-rebinding Host allowlist, ingest `X-Ingest-Token`. No new secrets, no new endpoints. (User-confirmed in interview Round 2.)

## Success Criteria

1. `list_sleep_sessions` returns the full field set for recognized records. Exact live values (post-deploy): night ending 2026-09-28 — total 6.2614, deep 1.0430, core 3.7234, rem 1.4950, awake 0.2620, asleep 0, in_bed 0, start 2026-09-27 23:05:35 +0900, end 2026-09-28 05:49:54 +0900. Night ending 2026-09-17 — total 6.3746, deep 1.0473, core 3.5891, rem 1.7382, awake 0.05, start 2026-09-16 22:11:12 +0900, end 04:58:30 +0900. AutoSleep nap (day 2026-09-17) — total 0.5833, asleep 0.5833, in_bed 0.5833, start 2026-09-16 17:35:00 +0900.
2. Re-ingest idempotency: two consecutive `health-replay --dir data/archive --force` runs leave `sleep_sessions` counts unchanged; zero duplicates. Census: one row per sleep-shaped payload point (130 recent-daily + 2,500 backfill); only permitted drop = points without parseable `sleep_start`, each counted under `skipped.sleep_session`.
3. Existing emissions untouched: `tests/unit/test_census_lock.py` passes unchanged; night totals (9/27 = 7.6521 h, 9/28 = 6.2614 h) unchanged.
4. Route paging on workout `e5a1b2c3-4d5e-4f60-8a9b-0c1d2e3f4a5b`: `route_count` 2873; at `route_limit=1000` pages 1000/1000/1000/99 with `next_route_offset` 1000/2000/3000/None; summary fields identical (duration_sec 2864.40, avg_hr 137.85, max_hr 152, active_energy_kcal 496.74, distance_km 6.824, hr 54 points, recovery 24 entries).
5. `freshness_check.latest_activity_day` == `2026-09-28` on the current DB while nutrition metrics sit ≥30 days behind; `latest_complete_day`/`stale` otherwise unchanged.
6. `list_workouts` pagination: `from=2018-01-01, to=2018-12-31, limit=100` → pages 100/100/40, `next_offset` 100/200/None, total 240; no silent truncation.
7. Probe cleanup: `list_metrics` count 85 → 83; probes absent from `freshness_check`.
8. Regression: full suite green (unit + `requires_pg` acceptance), `ruff check` clean; 90-day `get_metric_series` perf < 1 s unchanged; ECG / heart-notification / raw_samples responses unchanged.
9. Post-deploy live verification via MCP on the server server, executed and documented in the run journal: items 1–3, route pagination round-trip, plus backfill-era spot check (night ending 2026-08-20 = 5.9714 h).

## Out of Scope

- No changes to ingest acceptance semantics, `raw_ingest`, or archive layout.
- No analysis features; no consumer-side code in this repo.
- No changes to `sleep_analysis`/`sleep_in_bed` emissions, `daily_rollups`, or existing tool response shapes beyond documented additive fields.
- No removal of Apple nutrition metrics (expected staleness).
- No removal of archived probe payloads (only DB rows); a payload purge may follow separately.
- Legacy `predecessor` retirement — consumer side, gated on this run's criteria.
- TLS/backup hardening (existing doctrine defers).

## Test Strategy

- **Unit (pytest, no docker):** normalizer `_sleep_session_rows` — full-field extraction for all 5 `sleep_shape_matrix.json` shapes + the two demo full records above; `no_totalsleep_shape` (no sleepStart) → skip + `skipped.sleep_session` count; session emission co-exists with existing emissions (census lock untouched); route-paging parameter clamps; `latest_activity_day` derivation (incl. missing-metric → None); `list_workouts` pagination boundaries + last-page marker; CLI `health sleep` round trip (tests/unit/test_cli.py).
- **Acceptance (`requires_pg`, disposable postgres:18):** ingest representative-payload fixture → `list_sleep_sessions` round trip with exact §1 values; double replay idempotency; route pagination walk (a synthesized route set in the acceptance fixture — the committed `workout_v2_sample.json` carries only 5 points, so the acceptance test generates a large synthetic route page set; the 2,873-point / exact-summary walk is live-server verification, criterion 4); perf gate non-regression (tests/acceptance/slice_2_perf.py).
- **Fixtures:** new `tests/fixtures/parse_everything/sleep_session_detail.json` (real numeric values; source names genericized per house fixture discipline — see existing sleep_shape_matrix.json `_provenance`). Do NOT modify `sleep_shape_matrix.json`. Replay-idempotency acceptance uses the local archive subset (27 payloads contain the full sleep corpus for their covered days) plus synthetic fixtures; the full 2,500-point backfill replay is live-server verification (criterion 2/9), not a local acceptance test.
- **Live verification (post-deploy, server):** success criteria 1–9 via MCP; results appended to run journal.

## Design Direction

No human-facing UI (project design.md: interaction surfaces are MCP tools + CLI, LLM-centric). Visual artifacts not applicable — this run is API/data-plane only; `.tenet/project/design.md` conventions govern: bounded tool outputs (the route-paging change exists precisely to enforce this), catalog-first grounding (`health://schema` gains the new table), errors as guidance, JSON to stdout / diagnostics to stderr for CLI.

## Must-not-change invariants (consumer depends on all)

- `day_kst` = KST calendar day (fixed +9, never a named zone); `from`/`to` KST-day filters.
- Per-source sleep rows never merged; AutoSleep rows kept (cross-check discipline).
- Rollup semantics + `is_cumulative` flags; per-day recompute on ingest; latest-wins upserts.
- `raw_samples` paging contract (default 1000 / max 10000, `next_offset` None on last page) + metric/from/to filters.
- `get_workout_detail` keeps: `hr` array, `heart_rate_recovery`, weather, cadence, step_count, speed, intensity, is_indoor, location, energies.
- ECG, heart-notification, sleep_in_bed tools unchanged. Bearer auth, archive-first, idempotency unchanged.

## Deploy + live verification

1. Land code + tests green; rebuild image; redeploy server container (`health-migrate` applies 0003 at startup).
2. `docker exec health-db health-replay --dir data/archive --force` — backfills sleep_sessions across full archived history; run TWICE (idempotency, criterion 2).
3. Run probe-metric cleanup SQL (§5) — after replay, so replay cannot resurrect probes.
4. Verify success criteria 1–9 live via MCP; append to run journal; update README (tool roster + CLI examples) and `health://schema` entry (code-side).
5. Consumer-side cutover follows from the consumer once criteria pass.