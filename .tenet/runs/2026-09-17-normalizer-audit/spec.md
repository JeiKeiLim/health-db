---
delivery_mode: autonomous
run: 2026-09-17-normalizer-audit
mode: standard
model_tier: local
---

# Spec: parse everything — sleep normalizer fix + full data-point coverage

## Purpose

Every data point visible in an HAE JSON v2 payload must be parsed and stored — no silent drops. Today the sleep-aggregated branch stores 0.0 for every Apple Watch night (`asleep: 0` sentinel treated as a value), and three data surfaces are silently ignored or dropped: in-bed-only sleep rows, the `data.ecg` / `data.heartRateNotifications` lanes, and non-canonical-unit rows (lb). This run fixes the sleep rule, adds the missing lanes with tables + MCP tools, converts convertible units, and repairs the three already-corrupted days on the server — all before the full-history backfill is ingested (server currently holds only 2026-09-15→17, verified live).

## Tech Stack

Unchanged from project doctrine (`.tenet/project/architecture.md`): Python 3.12+ (uv, psycopg3, FastAPI, mcp SDK), PostgreSQL 18, pytest + ruff, Docker deploy on Docker. No new dependencies.

## Root-cause summary (verified evidence, research/predecessor-inventory.md)

- `src/health_db/normalize.py:422-435` — sleep-aggregated branch: `asleep = _num(point.get("asleep"))`, `if asleep is not None: yield asleep`. Real REST Watch rows carry `asleep: 0` sentinel + `totalSleep > 0` (verified 120/120 recent nights + 1,150/1,200 backfill nights). Result: qty 0.0.
- 49/1,200 backfill Watch nights are mixed-stage: `totalSleep = core+deep+rem+asleep` exactly (verified 50/50) — `asleep` is a real stage there, so it must never be treated as a source marker.
- AutoSleep REST rows: naps only, disjoint from Watch nights (8 co-days, all disjoint windows) → no cross-source precedence needed; per-source rows + rollup SUM are correct.
- 520 backfill + 1 recent sleep rows have only `inBed > 0` (시계/iPhone era, no stage data).
- `data.ecg` (42 sessions, 600,000 voltage points) and `data.heartRateNotifications` (350 events, 1,900 samples) exist in the backfill payload; `src/health_db/payloads.py:_parse_document` (lines 174-208) reads only `data.metrics`/`data.workouts`, so both lanes are invisible.
- `src/health_db/normalize.py:342-355` `_qty_for_unit`: canonical-unit metrics with non-canonical declared units are skipped (iron law 4). Observed in corpus: `weight_body_mass` 1× lb. (kJ→kcal conversion already exists as the sanctioned exception.)
- Shape-recognition gap: `src/health_db/payloads.py:73` `_SLEEP_AGGREGATED_KEYS` requires all of {totalSleep, asleep, core, deep, rem}; a point with {asleep, core, date, deep, rem} (no totalSleep — observed once in archive corpus) is unrecognized and dropped by `normalize.py` dispatch (`"totalSleep" in keys` guard, line 422).

## Changes

### 1. Sleep value rule (normalize.py `_rows_for_point`, aggregated branch)

New precedence for sleep_analysis points (replaces asleep-preference):

```
qty = totalSleep if totalSleep > 0
      else asleep if asleep > 0
      else core+deep+rem if all three present and sum > 0
      else (inBed branch below)
      else SKIP → counted in skipped.sleep_no_value (§5)
```

(Precedence note: asleep precedes core+deep+rem — matching research §6/§7's universal rule; immaterial on the verified corpus — no row has totalSleep=0 with both real stages and asleep>0 — but the two documents now agree and tests lock THIS order.)

- In-bed handling: if `inBed > 0` and all sleep fields are 0/absent → emit a row under new metric `sleep_in_bed` (qty = inBed hr, unit hr, same source/ts semantics as sleep_analysis). If inBed > 0 AND sleep fields exist → emit both the sleep_analysis row and the sleep_in_bed row (in-bed time is independent of stage totals).
- `sleep_in_bed` registers in `metric_dim` as display "Sleep In-Bed", cumulative (unit hr), pass-through unit policy.
- Shape dispatch loosened: the aggregated-sleep branch triggers on any sleep-shaped point containing any of {totalSleep, asleep, core, deep, rem, inBed, sleepStart} alongside `date` — totalSleep no longer required. `payloads.py` `_SLEEP_AGGREGATED_KEYS` recognition loosened to the same sufficient-key-set rule (recognition = keys ⊇ {date} ∧ ∩ sleep field set ≠ ∅ ∧ no qty/startDate — preserving the existing unaggregated/common precedence).
- Universal-rule census (must be reproduced by tests as fixtures): recent 130 exports → 128 sleep_analysis rows kept, 2 in-bed splits; backfill → 2,200 kept (1,000 AutoSleep, 1,200 Watch), 300 in-bed splits.

### 2. ECG lane (new)

- `payloads.py` + `normalize.py` + `pipeline.py` consume `data.ecg`: session rows → new `ecgs` table; voltage points → new `ecg_voltages` child table.
- ECG point shape (verified from backfill): `{source, start, end, samplingFrequency, numberOfVoltageMeasurements, classification, averageHeartRate, voltageMeasurements: [{voltage, date, units: 'mcV'}]}`.
- Normalization: start/end via `_parse_ts`; voltage ts = **UNIX epoch seconds** → `datetime.fromtimestamp(date, tz=UTC)` (verified 42/42: first voltage ts == session start ±2s under unix interpretation; CF-absolute yields year-2056 — the predecessor CF epoch applies only to the retired .hae lane, NOT REST ECG); voltage qty stays mcV (pass-through).
- Rows missing a parseable start or with an empty voltageMeasurements array are skipped per-row and counted in `skipped.ecg` (§5).
- Upserts latest-wins: `ecgs` natural key (source_name, start_ts) UNIQUE; `ecg_voltages` PK (ecg_id, seq) where seq is the array index. No rollup integration (session lane, like workouts).

### Envelope note

Payloads carrying ONLY `data.ecg`/`data.heartRateNotifications` (no metrics/workouts) would still be rejected as an empty v2 envelope (payloads.py:186-187). No real corpus file is lane-only (verified 0/212 recent/workout-lane/pending files carry these lanes; they appear only in backfill alongside metrics+workouts), so the envelope rule stays as-is; the 400+quarantine path preserves any such future payload verbatim for replay.

### 3. Heart-rate-notification lane (new)

- Consume `data.heartRateNotifications`: event rows → new `heart_rate_notifications` table; each event's `heartRateData`/`hrvData` sample lists → new `heart_rate_notification_samples` child table (workout_heart_rate pattern).
- Event shape (verified): `{start (ISO Z), end, threshold, heartNotification ('high_heart_rate_notification'), source: {name, identifier}, heartRateData: [{heartRate, hrUnits, timestamp: {start, end, units: 's'}}], hrvData: [{hrv, units: 'ms', timestamp: {start, end, units: 's'}}]}` (HR value key = `heartRate`, unit `hrUnits`; HRV value key = `hrv`, unit `units` — both pinned from the real corpus). Sample ts = `timestamp.start` (ISO string → `_parse_ts`).
- Natural keys: events UNIQUE (source_name, start_ts); samples PK (notification_id, ts, kind) with kind ∈ {'hr','hrv'} (a single ts may carry both an HR and an HRV sample — hence kind in the PK). Latest-wins upserts. No rollup integration.

### 4. Unit-conversion amendment (iron law 4, user-approved)

- `normalize.py`: for metrics in `CANONICAL_UNITS`, a non-canonical declared unit is CONVERTED when an exact factor exists instead of skipped: lb→kg ÷ 2.20462, st→kg ÷ 0.157473, kJ→kcal ÷ 4.184 (existing), °F→°C (°F−32)×5/9 for `apple_sleeping_wrist_temperature`. Factors as named constants next to `KJ_PER_KCAL` (normalize.py:59).
- Unconvertible non-canonical units (no exact factor, e.g. a future unit) are still skipped — but counted and surfaced (§5), never silent.
- README iron-law wording + `.tenet` doctrine drift note: iron law 4 becomes "convert-with-exact-factor, else skip-visibly" (journal note, not direct doctrine edit).
- `metric_dim` seeds unchanged (canonical unit still the registered unit; converted rows store the canonical unit string).

### 5. Visible skip accounting (ingest response)

- `PayloadReport` (src/health_db/payloads.py:80-95) gains a `skipped: dict[str, int]` field with exact keys: `"unit_unconvertible"`, `"unrecognized_shape"` (mirrors unknown_formats, count only), `"ecg"` (rows dropped: unparseable start / empty voltages), `"notification"` (rows dropped: unparseable start), `"sleep_no_value"` (sleep-shaped points with all-zero fields and inBed=0 → no row, counted). All counters default to 0; the object is always present.
- `/api/ingest` 200 response body gains `"skipped": {...}` (from the report; `IngestResult` carries it through pipeline.ingest_payload's return path unchanged — response layer composes from the report it already holds). The archive-only (pool-None) response in main.py:242-255 includes it identically. Replay/reduce CLI output prints the same object to stderr as diagnostics.
- Zero silent drops is the observable contract: every dropped point increments exactly one counter.

### 6. Schema — migration `migrations/0002_parse_everything.sql` (additive only)

| Table | Column | Type | Constraints |
|---|---|---|---|
| ecgs | id | bigint GENERATED ALWAYS AS IDENTITY | PK |
| ecgs | source_name | text | NOT NULL |
| ecgs | start_ts | timestamptz | NOT NULL |
| ecgs | end_ts | timestamptz | NOT NULL |
| ecgs | day_kst | date | generated (same expression as raw_samples, 0001_init.sql:50-51) |
| ecgs | classification | text | |
| ecgs | sampling_frequency | integer | |
| ecgs | average_heart_rate | integer | |
| ecgs | voltage_count | integer | NOT NULL |
| ecgs | ingested_at | timestamptz | NOT NULL DEFAULT now() |
| ecgs | | | UNIQUE (source_name, start_ts) |
| ecg_voltages | ecg_id | bigint | FK ecgs ON DELETE CASCADE |
| ecg_voltages | seq | integer | NOT NULL |
| ecg_voltages | ts | timestamptz | NOT NULL |
| ecg_voltages | voltage | double precision | NOT NULL |
| ecg_voltages | units | text | NOT NULL |
| ecg_voltages | | | PK (ecg_id, seq) |
| heart_rate_notifications | id | bigint GENERATED ALWAYS AS IDENTITY | PK |
| heart_rate_notifications | source_name | text | NOT NULL |
| heart_rate_notifications | start_ts | timestamptz | NOT NULL |
| heart_rate_notifications | end_ts | timestamptz | NOT NULL |
| heart_rate_notifications | day_kst | date | generated |
| heart_rate_notifications | kind | text | NOT NULL |
| heart_rate_notifications | threshold | integer | |
| heart_rate_notifications | ingested_at | timestamptz | NOT NULL DEFAULT now() |
| heart_rate_notifications | | | UNIQUE (source_name, start_ts) |
| heart_rate_notification_samples | notification_id | bigint | FK ON DELETE CASCADE |
| heart_rate_notification_samples | ts | timestamptz | NOT NULL |
| heart_rate_notification_samples | kind | text | NOT NULL ('hr' or 'hrv') |
| heart_rate_notification_samples | value | double precision | NOT NULL |
| heart_rate_notification_samples | units | text | |
| heart_rate_notification_samples | | | PK (notification_id, ts, kind) |

- Migration applies inside the existing versioned applier (one transaction; `health-migrate` runs at container start, entrypoint contract per README §Deployment).
- No changes to raw_samples / daily_rollups / workouts tables.

### 7. MCP tools (new, on the same server, Bearer auth inherited)

| Tool | Args | Returns |
|---|---|---|
| `list_ecgs` | from?, to?, limit (default 50, max 500) | session rows (id, source, start/end, classification, avg HR, voltage_count) |
| `get_ecg_detail` | id, voltage_offset?, voltage_limit? (default 1000, max 10000) | session metadata + paged voltage samples |
| `list_heart_notifications` | from?, to?, limit (default 50, max 500) | event rows + nested sample rows |
| `list_sleep_in_bed` is NOT added — `sleep_in_bed` is a plain metric: `query_day` / `get_metric_series` / `get_trends` / `raw_samples` already cover it via metric_dim (verified: queries.py resolves any metric_dim row). |

- `sleep_in_bed` registration: added to `normalize.DISPLAY_NAMES` as "Sleep In-Bed" (normalize.py:98-181 map); deliberately NOT in `CANONICAL_UNITS` (pass-through unit policy, is_cumulative seeded from unit 'hr' via existing CUMULATIVE_UNITS logic, normalize.py:88).

- CLI: `health ecgs`, `health ecg-detail <id>`, `health hrn` mirror the tools (thin wrapper convention, src/health_db/cli.py).
- `health://schema` resource (queries.py:759 `schema_resource`) updated to include the new tables.

### 8. Repair of the 3 live days + deploy sequencing

1. Land all code + tests; merge; rebuild image; redeploy server container (health-migrate applies 0002).
2. `docker exec health-db health-replay --dir data/archive --force` — re-normalizes the archived payloads, rewriting the sleep 0.0 rows (e.g. 2026-09-15: 0.0 → 6.21; 2026-09-16: 0.0 → 6.34; 2026-09-17: 0.0 → 6.37) and recomputing rollups in the same transactions (force path verified in src/health_db/pipeline.py:203-302 + replay.py).
3. THEN run the full-history backfill per README §Backfill runbook (all new lanes + conversions exercised on first ingestion).

## API Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | /api/ingest | X-Ingest-Token | unchanged, response body adds `skipped` object |
| GET | /api/health | none | unchanged |
| * | /mcp (tools) | Bearer | + list_ecgs, get_ecg_detail, list_heart_notifications |

## Auth Flow

Unchanged: ingest token header (config INGEST_SECRET/INGEST_HEADER), MCP Bearer (same secret), 401 + auth_log audit on failure (src/health_db/auth.py). New MCP tools inherit Bearer; no new credentials.

## Success Criteria

1. **Census-DoD (user-confirmed)**: replaying the full audited corpus — 130 daily exports (420,000 points), 76 workout-lane files (166 workouts), 20 archived server payloads, 3 pending, 1 backfill file (52,000 metric points, 1,600 workouts, 42 ECG sessions + 600,000 voltages, 350 notifications + 1,900 samples, 2,900 sleep rows) — yields exactly zero silently-dropped points: every point maps to a stored row or appears in the response's `skipped` accounting with a reason.
2. Sleep values: replaying the 130 dailies produces 128 sleep_analysis rows (nights + naps, values 0.2–8.2 hr matching payload totals) + in-bed rows; zero qty=0.0 sleep rows. Fixture-locked: Watch `asleep:0` night → totalSleep; 50 mixed-stage nights → totalSleep (not core+deep+rem); AutoSleep nap → asleep; in-bed-only row → sleep_in_bed metric.
3. Backfill sleep parity: 2,200/2,500 sleep rows kept + 300 sleep_in_bed rows (the 300+2 all-zero rows split), matching the census numbers exactly.
4. ECG/notification ingest: posting the backfill file stores 42 ecgs, 600,000 ecg_voltages, 350 notifications, 1,900 samples; re-post is idempotent (latest-wins, zero new rows).
5. Unit conversion: the archived lb weight payload (archived-lb-export) converts to 72.57 kg and stores under weight_body_mass unit kg; kJ rows unchanged; a hypothetical 'st' row converts ÷ 0.157473.
6. Ingest response: `skipped` object present, accurate, and testable (unit-unconvertible case fixture).
7. MCP: `list_ecgs`/`get_ecg_detail`/`list_heart_notifications` return correct data (acceptance-tested against the backfill fixture); `health://schema` lists new tables; 90-day `get_metric_series` still < 1s (tests/acceptance/slice_2_perf.py must not regress).
8. Replay performance: full backfill-file ingest + force-replay of the 20-payload archive corpus completes within the existing test-suite time budget; ECG voltage inserts batched (≤10k/executemany).
9. Repair: after server redeploy + force-replay, `query_day 2026-09-15..17 sleep_analysis` returns 6.21/6.34/6.37 (sum), verified via MCP against the live server (manual verification step documented in run journal).
10. All existing tests stay green; ruff clean; new unit + acceptance tests land with the code (project testing doctrine).

## Out of Scope

- .hae/lzfse consumption (REST JSON only — predecessor lanes stay retired)
- Cross-source sleep precedence layers (evidence: disjoint windows; SUM correct)
- daily_rollups integration for ECG/notifications (session lanes, like workouts)
- TLS/port-forwarding hardening, backup cron (existing doctrine defers these)
- Analysis features, multi-user, UI
- Converting predecessor SQLite history (backfill comes from HAE, never SQLite)

## Design Direction

No visual artifacts (data-plane change; MCP/CLI surfaces already designed in `.tenet/project/design.md — catalog-first, summaries-by-default, errors-as-guidance`). New tools follow the same pagination/limit conventions as `raw_samples`/`get_workout_detail` (bounded defaults, explicit limits, actionable errors).

## Test Strategy

- Unit (pure normalizer/payloads): sleep rule matrix (7 fixture shapes from real corpora), in-bed split, loosened recognition, ECG/notification normalization (CF-epoch ts), unit conversion + unconvertible accounting.
- Acceptance (requires_pg, disposable postgres:18 container per existing conftest): full-pipeline ingest of sanitized representative-payload fixtures (including a trimmed ECG/notification backfill fixture), upsert idempotency, MCP tool round-trips, perf gate non-regression, replay force-path re-normalization.
- Fixtures: sanitized real payloads committed under tests/fixtures (PII-scrubbed per existing fixture discipline); the 300 MB backfill file itself stays on private cloud storage, NEVER committed.