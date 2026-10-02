# Research: HAE JSON v2 export format (metrics + workouts) [scanned-not-verified]

Sources: help.healthyapps.dev export-format pages (fetched 2026-09-08), predecessor repo code (constants.py, normalizer.py).

## Key findings

- **Top-level**: `{"data": {"metrics": [...], "workouts": [...], ...}}`; v2 is the current export version (HAE automation setting).
- **Metric names are snake_case in REST exports** (`step_count`, `heart_rate`). predecessor has an 80-entry `METRIC_NAME_MAP` (snake_case → CamelCase) at `src/health_pipeline/constants.py:21-102`. For health-db we can store the native snake_case name in `metric_dim` and keep a display_name; rollups key on the dimension id — name collisions between lanes are a non-issue in the new single-lane REST world.
- **Common metric sample**: `{qty, date, source?}` with group-level `{name, units, data: []}`; date format `yyyy-MM-dd HH:mm:ss Z` (offset included). **Source may be omitted on iOS 27+ for cumulative and heart rate data** — dedup key must tolerate missing source (normalize to empty string/'unknown').
- **Special formats** (must be handled by the normalizer):
  - blood_pressure: `{systolic, diastolic, date}` (no qty)
  - heart_rate: `{Min, Avg, Max, date}` (no qty; Avg is the canonical value)
  - sleep_analysis (aggregated): `{date (day-level), totalSleep, asleep, core, deep, rem, sleepStart, sleepEnd, inBed, inBedStart, inBedEnd}` — units hr
  - sleep_analysis (unaggregated): `{startDate, endDate, qty, value (Awake/Asleep/In Bed/Core/REM/Deep/Unspecified), ...}` — sleepStart/End + inBedStart/End present
  - blood_glucose (+mealTime), sexual_activity (protection counts), handwashing/toothbrushing (+value Complete/Incomplete), insulin_delivery (+reason Bolus/Basal)
- predecessor normalizer already handles most of these shapes from the JSON lane (qty|totalSleep|Avg extraction; sleep `asleep`-preference rule from TASK-005: if any sample in file has `asleep`, only `asleep` kept — AutoSleep wins over Apple Watch stages).

## Canonical units & conversions (grounded in predecessor constants.py:104-119)

kg, %, m, km, km/hr, m/s, cm, kcal (kJ→kcal ÷4.184), degC; non-canonical-unit samples skipped (not converted) — LOCKED decision 2026-08-03. Cumulative units for trends sum-vs-avg: {"count","km","kcal","kJ","min","hr","mg","g","mcg"}.

## Workouts v2 (help.healthyapps.dev export-format/workouts)

- Fields: id (UUID string), name, start/end dates, timezoneOffset, duration, activityConfig, isIndoor, location, distance?, activeEnergy?, totalEnergy?, basalEnergy?, elevation?, avgHeartRate?, maxHeartRate?, heartRateRecovery?, heartRateData? (per-minute `[{date, bpm}]` when Include Workout Metrics ON), route? (`[{timestamp, latitude, longitude, course, speed, altitude, horizontalAccuracy, verticalAccuracy}]` when Include Route Data ON), stepCount data, METs (JSON lane in predecessor sets mets=None; .hae lane had mets).
- energy in kJ in v2 JSON (predecessor converts activeEnergy/4.184 → kcal at normalize).

## Server-side implications for health-db

1. Normalizer input = REST payload only; single lane; latest-wins on (metric, ts, source) natural key.
2. Special formats each map to qty or dedicated columns (blood pressure → two metrics or systolic/diastolic columns; heart_rate Avg as qty; sleep aggregated → day-level record with asleep-preference).
3. Content-hash on raw payload → skip identical re-POSTs (idempotency; healthlog pattern).
4. Payload cap: healthlog uses 32 MiB default; our automation payloads are small deltas; Manual Export backfill chunks need headroom — accept up to 64 MiB, configurable.
5. Batch Requests: HAE splits large exports into multiple POSTs; each is processed independently — ingest must be order-independent (latest-wins makes this safe).

Confidence: payload shapes from official docs [scanned-not-verified until slice-1 real payloads captured]; normalizer semantics from predecessor code [implemented-and-tested there, to be re-tested here with captured fixtures].