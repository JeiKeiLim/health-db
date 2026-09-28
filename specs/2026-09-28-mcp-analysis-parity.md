---
delivery_mode: autonomous
spec: mcp-analysis-parity
date: 2026-09-28
repo: health-db
status: proposed
prepared_by: Max (Hermes health agent — primary MCP consumer)
---

# Spec: MCP analysis parity — sleep-session detail, route paging, freshness activity day

Consumer-side brief for the health-db developer agent. Written after a full live audit of the MCP surface (2026-09-28, repo HEAD `dd668f8`), by the primary MCP client — the Hermes "Max" health agent. Goal: close the last capability gaps that still force the consumer to fall back to the legacy `predecessor` SQLite pipeline, so that pipeline can be retired.

## Purpose

After this run the health-db MCP surface fully covers the consumer's daily and weekly analysis flows — sleep structure (stages + real bed/wake windows), workout detail without client-context floods, and an activity-only freshness marker — with zero reliance on the legacy local pipeline. Two small hygiene items (workout-list paging, probe-metric cleanup) are included because they hit the same flows.

## Tech Stack

Unchanged from project doctrine: Python 3.12+ (uv), psycopg3, FastAPI, mcp SDK v2, PostgreSQL 18, pytest + ruff, Docker. **No new dependencies.** One additive migration (`0003`); no ALTER/DROP on existing tables (0001/0002 frozen; additive-only rule from run 2026-09-17-normalizer-audit).

## Verified context (live audit, 2026-09-28)

- All 11 MCP tools reply; parity spot-checks against the legacy pipeline pass (9/27 day: sleep 7.6521 h, steps 10,472.83, RHR 57, active energy 716.4 kcal, HRV sum 521.44; 9/27 run summary matches to the digit).
- History depth confirmed: workouts back to 2018 (240 rows for 2018; 239 for 2026 YTD); raw samples verified back to 2019 (step_count) and 2024 (heart_rate).
- Freshness model works: REST push lands data same-day — the 9/28 strength session was in health-db before the legacy pipeline saw it.
- Remaining gaps are exactly the items in **Changes**; everything else the consumer needs is already served.

## Changes

### 1. Sleep-session detail lane (P1 — the cutover blocker)

**Current behavior.** The normalizer's aggregated-sleep branch (`src/health_db/normalize.py`, `_rows_for_point`, sleep branch) emits only the value row (`sleep_analysis` qty) + optional `sleep_in_bed` row. Every other payload field — `sleepStart`, `sleepEnd`, `inBedStart`, `inBedEnd`, `deep`, `core`, `rem`, `awake` — is evaluated for precedence and then dropped. Result: the consumer must read raw export JSON files for stage data and bed/wake anchors (the 9/28 morning brief read deep/core/rem + sleepStart/sleepEnd straight from the cloud-synced lane file — exactly the dependency this run removes).

**New: a `sleep_sessions` session lane** — same architectural pattern as `ecgs` / `heart_rate_notifications` (migration + pipeline store stage + query layer + MCP tool + CLI + schema resource), OUT of `daily_rollups`.

- **One row per recognized sleep record** — every payload point that today yields a `sleep_analysis` or `sleep_in_bed` row (Watch nights, AutoSleep naps/dozes, in-bed-era rows). Per-source rows are **never merged**. Nap/doze segments stay distinct records; the consumer applies its own nap exclusion (starts 16:00–18:59) — the lane must NOT filter them.
- **Fields** (verbatim from payload; all durations in hours): `source_name`, `record_date`, `day_kst`, `sleep_start`, `sleep_end`, `in_bed_start`, `in_bed_end`, `total_sleep`, `asleep`, `deep`, `core`, `rem`, `awake`, `in_bed`.
- **Semantics:**
  - `record_date` = the payload's `date` field verbatim (KST midnight of the attributed day). `day_kst` is generated from it (same expression family as `raw_samples.day_kst`) — bucketing MUST be identical to the existing `sleep_analysis` rows (for a main night: the morning the sleep ENDED).
  - `asleep: 0` on Watch rows is the known sentinel (never "no sleep"); store verbatim. Modern-row stage truth = `deep`/`core`/`rem` (+`awake`). Mixed-stage rows (49 in backfill) carry a real `asleep` stage — store verbatim, no re-derivation.
  - Rows lacking a parseable `sleep_start` are **skipped and counted** in the ingest response `skipped` object under a new additive key `sleep_session` (zero-silent-drops doctrine). Missing `sleep_end`/in-bed fields stay NULL.
  - Upsert latest-wins on `UNIQUE (source_name, sleep_start)`; re-ingest fully idempotent.
- **Existing `sleep_analysis` / `sleep_in_bed` metric emissions are NOT touched** — trends, rollups, and the census lock (`tests/unit/test_census_lock.py`) must stay valid unchanged.
- **MCP tool `list_sleep_sessions(from?, to?, limit=100)`** — newest first, KST-day range filter on `day_kst`, `limit` clamped `[1, 500]`, response rows `{id, source, day, sleep_start, sleep_end, in_bed_start, in_bed_end, total_sleep, asleep, deep, core, rem, awake, in_bed}` with timestamps in the house `_ts_iso` convention (ISO-8601, like every other tool).
- **CLI mirror:** `health sleep --from YYYY-MM-DD [--to YYYY-MM-DD] [--limit N]`.
- `health://schema` gains the `sleep_sessions` table entry.

**Real payload excerpts (verbatim; values must be preserved through ingest):**

Night 2026-09-27→28:
```json
{"date": "2026-09-28 00:00:00 +0900", "source": "Jongkuk의 Apple Watch",
 "sleepStart": "2026-09-27 23:05:35 +0900", "sleepEnd": "2026-09-28 05:49:54 +0900",
 "inBedStart": "2026-09-27 23:05:35 +0900", "inBedEnd": "2026-09-28 05:49:54 +0900",
 "totalSleep": 6.2614, "asleep": 0, "inBed": 0, "awake": 0.262,
 "core": 3.7234, "deep": 1.043, "rem": 1.495}
```

Night 2026-09-16→17 + AutoSleep nap (same export file — two records, never merged):
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

**Consumer usage example:**
```
list_sleep_sessions(from="2026-09-28", to="2026-09-28")
→ night ending 9/28: start 23:05:35 (prev day) / end 05:49:54, total 6.2614 h,
  deep 1.0430 / core 3.7234 / rem 1.4950 / awake 0.2620, source Apple Watch
```

### 2. `get_workout_detail` route paging (P2)

**Current behavior.** `get_workout_detail` (`src/health_db/queries.py`) returns every route point inline — verified live: **2,873 points ≈ 1.0 MB for one 7 km run**, which floods an MCP client's context (same failure class the legacy `health workouts` command had; the fix there was separate route commands).

**Change — mirror the `get_ecg_detail` paging pattern exactly:**
- Signature: `get_workout_detail(id, route_offset=0, route_limit=1000)`; `route_limit` clamps `[1, 10000]`.
- Response adds: `route_count` (total points), `route_offset`, `route_limit`, `next_route_offset` (`None` on last page).
- `hr` and `heart_rate_recovery` stay inline and unpaged (small, always needed).
- CLI `health routes <id>` gains the same offset/limit options.
- The consumer explicitly accepts the change: routes beyond the first page are fetched via `next_route_offset` when a run-efficiency analysis needs them.

### 3. `freshness_check` — `latest_activity_day` (P2)

**Change (purely additive):** add `latest_activity_day` to `freshness_check` and the `health://freshness` resource = `max(last_day)` over exactly these metrics: `sleep_analysis`, `heart_rate_variability`, `resting_heart_rate`, `step_count`, `active_energy`, `apple_exercise_time`, `walking_running_distance`. Missing metrics are ignored; `None` when none exist.

`latest_complete_day` (median consensus) and `stale` semantics stay **unchanged** — the nutrition feed legitimately pins the median (see §6), so the consumer needs a separate activity-only marker to date-label reports without deriving it client-side. (It can derive it today, but a first-class field kills a class of cron-side errors.)

### 4. `list_workouts` pagination (P3)

**Current behavior.** Hardcoded `LIMIT 1000`, no marker — ranges yielding >1000 rows are silently truncated (`src/health_db/queries.py`, `list_workouts`).

**Change:** add `limit` (default 500, clamp `[1, 2000]`) and `offset`; response adds `limit`, `offset`, `next_offset` (`None` = last page). Keep existing `count` field (rows in this page).

### 5. Probe-metric cleanup (P3 — ops only)

Remove the test artifacts `__probe_metric` and `__probe_metric2` (auto-registered by the ingest's unknown-metric acceptance; last days 2026-01-09 / 2026-01-02) from the production DB — `metric_dim`, `daily_rollups`, `raw_samples` — via container psql:

```sql
BEGIN;
DELETE FROM daily_rollups WHERE metric_id IN
  (SELECT metric_id FROM metric_dim WHERE name IN ('__probe_metric','__probe_metric2'));
DELETE FROM raw_samples   WHERE metric_id IN
  (SELECT metric_id FROM metric_dim WHERE name IN ('__probe_metric','__probe_metric2'));
DELETE FROM metric_dim    WHERE name IN ('__probe_metric','__probe_metric2');
COMMIT;
```

Payloads stay in the archive (`raw_ingest` + disk) — a future full replay would resurrect the rows; acceptable, or purge the matching archived payloads as a follow-up. Not a migration (data cleanup, not schema).

### 6. Observations — no code change requested (confirm-only)

- **HR `source` = "unknown" on recent rows:** the HAE metrics-lane heart_rate point carries no source field (verified: `{"Min":62,"Avg":62,"Max":62,"date":...}` — no source key). Nothing to fix; the consumer does not filter HR by source. If a source ever appears upstream, the existing `source_dim` path will pick it up.
- **HR density is higher than the legacy pipeline (confirm intended):** same day 9/27: 864 raw rows vs 287 in the legacy DB — the difference is concentrated in the workout hour (612 vs 51), i.e. workout-HR likely also registers under the `heart_rate` metric. Unique timestamps verified, windowed averages match (sleep window avg 60.35 vs legacy 60.34). The consumer wants to **keep** this density (better sleep/pre-bed windows). Confirm it is intended; if it is not, flag it before removing.
- **Apple nutrition metrics** (`dietary_energy`, `protein`, …) remain stale by automation design; the consumer uses the Obsidian food log instead. Expected staleness — do not "fix", no change wanted.

## Migration `0003_sleep_sessions.sql` (additive only)

| Table | Column | Type | Constraints |
|---|---|---|---|
| sleep_sessions | id | bigint GENERATED ALWAYS AS IDENTITY | PK |
| sleep_sessions | source_name | text | NOT NULL |
| sleep_sessions | record_date | timestamptz | NOT NULL (payload `date` verbatim) |
| sleep_sessions | day_kst | date | GENERATED ALWAYS — KST day of `record_date` (fixed +9h expression, same family as 0001) |
| sleep_sessions | sleep_start | timestamptz | NOT NULL (upsert key with source_name) |
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

Plus: `CREATE INDEX sleep_sessions_day_kst_idx ON sleep_sessions (day_kst);` — sessions lane, no partition DDL, no rollup integration.

## API surface delta

| Surface | Change | Auth |
|---|---|---|
| MCP tool `list_sleep_sessions(from?, to?, limit=100)` | NEW | Bearer (inherited) |
| MCP tool `get_workout_detail(id, route_offset?, route_limit?)` | route paging added (additive fields) | Bearer |
| MCP tool `freshness_check()` | + `latest_activity_day` | Bearer |
| MCP tool `list_workouts(from?, to?, limit?, offset?)` | pagination added | Bearer |
| Resource `health://schema` | + sleep_sessions | Bearer |
| CLI `health sleep` | NEW (mirrors tool) | PSQL env |
| CLI `health routes <id>` | + offset/limit options | PSQL env |
| Ingest `/api/ingest` | unchanged rows; `skipped` gains additive key `sleep_session` | X-Ingest-Token |

## Success Criteria

1. `list_sleep_sessions` returns the full field set for recognized records. Exact live values (post-deploy):
   - Night ending 2026-09-28: total 6.2614, deep 1.0430, core 3.7234, rem 1.4950, awake 0.2620, asleep 0, in_bed 0, start 2026-09-27 23:05:35 +0900, end 2026-09-28 05:49:54 +0900.
   - Night ending 2026-09-17: total 6.3746, deep 1.0473, core 3.5891, rem 1.7382, awake 0.05, start 2026-09-16 22:11:12 +0900, end 2026-09-17 04:58:30 +0900.
   - AutoSleep nap (day 2026-09-17): total 0.5833, asleep 0.5833, in_bed 0.5833, start 2026-09-16 17:35:00 +0900.
2. Re-ingest idempotency: two consecutive `health-replay --dir data/archive --force` runs leave `sleep_sessions` counts unchanged; zero duplicates. Census: one row per sleep-shaped payload point (audited corpora: 130 recent-daily + 2,500 backfill points); the only permitted drops are points without a parseable `sleep_start`, each counted under `skipped.sleep_session`.
3. The lane does NOT alter existing emissions: `tests/unit/test_census_lock.py` passes untouched; night totals (9/27 = 7.6521 h, 9/28 = 6.2614 h) unchanged.
4. Route paging on workout `e5a1b2c3-4d5e-4f60-8a9b-0c1d2e3f4a5b`: `route_count` 2873; at `route_limit=1000` pages 1000/1000/1000/99 with `next_route_offset` 1000/2000/3000/None; summary fields identical to pre-change (duration_sec 2864.40, avg_hr 137.85, max_hr 152, active_energy_kcal 496.74, distance_km 6.824, hr 54 points, recovery 24 entries).
5. `freshness_check.latest_activity_day` == `2026-09-28` on the current DB while nutrition metrics sit ≥30 days behind; `latest_complete_day`/`stale` behavior otherwise unchanged.
6. `list_workouts` pagination: `from=2018-01-01, to=2018-12-31, limit=100` → pages 100/100/40, `next_offset` 100/200/None, total 240; no silent truncation for ranges exceeding the limit.
7. Probe cleanup: `list_metrics` count 85 → 83; probes absent from `freshness_check`.
8. Regression: full test suite green (unit + `requires_pg` acceptance), `ruff check` clean; 90-day `get_metric_series` perf < 1 s unchanged; ECG / heart-notification / raw_samples responses unchanged.
9. Post-deploy live verification via MCP on the server server, executed and documented in the run journal: items 1–3 above, route pagination round-trip, plus one backfill-era spot check (night ending 2026-08-20 = 5.9714 h).

## Must-not-change invariants (the consumer depends on all of these)

- `day_kst` = KST calendar day (fixed +9, never a named zone); `from`/`to` remain KST-day filters.
- Per-source sleep rows never merged; AutoSleep rows kept (cross-check discipline).
- Rollup semantics `sum/avg/min/max/count` + `is_cumulative` flags; per-day recompute on ingest; latest-wins upserts.
- `raw_samples` paging contract (default 1000 / max 10000, `next_offset` None on last page) + metric/from/to filters.
- `get_workout_detail` keeps: `hr` array, `heart_rate_recovery`, weather (`temperature`/`humidity`), cadence, `step_count`, `speed`, `intensity`, `is_indoor`, `location`, energies.
- ECG, heart-notification, sleep_in_bed tools unchanged. Bearer auth, archive-first, idempotency unchanged.

## Out of Scope

- No changes to ingest acceptance semantics, `raw_ingest`, or archive layout.
- No analysis features; no consumer-side code in this repo.
- No changes to `sleep_analysis` / `sleep_in_bed` emissions, `daily_rollups`, or existing tool response shapes beyond the documented additive fields.
- No removal of the Apple nutrition metrics (expected staleness; consumer ignores them).
- Legacy `predecessor` retirement — consumer side, gated on this run's success criteria.
- TLS/backup hardening (existing doctrine defers).

## Test Strategy

- **Unit:** normalizer emits a sleep-session row for every census shape with full-field extraction (5 shapes from `sleep_shape_matrix.json` + the two demo full records above); missing-`sleep_start` skip+count; route paging parameter validation/clamping; `latest_activity_day` derivation (incl. missing-metric cases); `list_workouts` pagination boundaries and last-page marker.
- **Acceptance (`requires_pg`):** ingest of a representative-payload fixture → `list_sleep_sessions` round trip with the exact §1 values; double replay idempotency; route pagination across all four pages summing 2873; perf gate non-regression.
- **Fixtures:** new `tests/fixtures/parse_everything/sleep_session_detail.json` (real numeric values; source names genericized per house fixture discipline). Do **not** modify `sleep_shape_matrix.json` — the census lock depends on it.
- **CLI:** `health sleep` round trip added to `tests/unit/test_cli.py`.

## Deploy + live verification

1. Land code + tests green; rebuild image; redeploy server container (`health-migrate` applies 0003).
2. `docker exec health-db health-replay --dir data/archive --force` — backfills `sleep_sessions` across the full archived history; run twice (idempotency check, success criterion 2).
3. Run the probe-metric cleanup SQL (§5).
4. Verify success criteria 1–9 live via MCP; append results to the run journal; update README (tool roster + CLI examples) and `health://schema`.
5. Consumer-side cutover (cron prompts + skills switch to MCP-only; legacy pipeline retirement) follows from the consumer once criteria pass.
