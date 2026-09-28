# Research: sleep_sessions lane implementation surface (pre-spec)

What: verify every codebase claim the spec depends on, and pin the exact
house patterns the new lane must mirror. Researched by the orchestrator
2026-09-28 against repo HEAD `dd668f8` — all claims below were read from the
real files (no assumptions).

## Findings (all verified against source)

### Lane pattern (the pattern sleep_sessions MUST mirror)
- `ecgs` / `heart_rate_notifications` lanes: migration `0002_parse_everything.sql`
  creates tables (additive-only); normalizer emits `_ecg_rows(parsed)` /
  `_hrn_rows(parsed)` returning `(sessions, voltages, skips)`; pipeline
  `_upsert_ecg_lane` / `_upsert_hrn_lane` (pipeline.py:239, :294) upsert
  latest-wins on natural key `(source_name, start_ts)` RETURNING id, then
  child rows by FK; `skipped` census merges via `merge_skipped` (payloads.py:101).
- Skip accounting: `SKIPPED_KEYS` tuple in payloads.py:82-88 is the frozen
  response contract — new key `sleep_session` is added to this tuple
  (additive, safe: `merge_skipped` ignores unknown keys defensively, old
  callers see a new key defaulting to 0).
- MCP + CLI both wrap the shared `queries.py` read layer (design.md
  contract; mcp_server.py holds no SQL).

### ECG paging pattern (the pattern get_workout_detail route paging MUST mirror)
- `get_ecg_detail(ecg_id, voltage_offset=0, voltage_limit=None)`:
  queries.py:705. `_bounded_limit(limit, DEFAULT, MAX, name)` clamps
  `[1, MAX]`; `_validated_offset`; fetch `limit+1` rows → `has_more` →
  `next_voltage_offset = offset+limit if has_more else None`. Route paging
  reuses `_bounded_limit`/`_validated_offset` verbatim with
  DEFAULT=1000, MAX=10000.

### Existing behaviors the spec's "current behavior" claims
- Aggregated sleep branch: normalize.py:635-676 — emits value row
  (precedence totalSleep>0 → asleep>0 → core+deep+rem) + optional
  SLEEP_IN_BED row; drops sleepStart/End, inBedStart/End, deep/core/rem/awake.
- get_workout_detail: queries.py:509-600 — route rows fetched with NO
  limit (queries.py:550-558), all inlined into response (`route` array).
- list_workouts: queries.py:470-506 — hardcoded `LIMIT 1000` (:484), no
  offset, no marker.
- freshness_check: queries.py:189+ — `latest_complete_day` median consensus
  only; no activity-day field.
- `_ts_iso` timestamp convention used by every tool (queries.py:494 etc).

### Corpus facts (archive verification, 2026-09-28)
- 2,500 sleep points total in the 2026-09-28 backfill archive file
  (export-b.json): 100% carry sleepStart/sleepEnd; 1,200 with real
  stages; 1,140 with awake>0; AutoSleep naps co-exist with Watch nights
  (e.g. 2026-09-16 nap 17:35→18:10 alongside Watch night 22:11→04:58).
- Recent-daily corpus: 130 sleep points (120 Watch sentinel + 8 AutoSleep
  + 2 in-bed-only) — census-locked by tests/unit/test_census_lock.py.
- The `sleep_shape_matrix.json` fixture points ALL carry sleepStart/sleepEnd
  (except `no_totalsleep_shape` which carries none) — the session lane's
  skip+count path (no parseable sleep_start) is exercised by that shape.

### Census lock constraint
- tests/unit/test_census_lock.py asserts sum(census_weight × outcome) ==
  locked numbers per lane, AND expected_qty per shape (sentinel regression
  detector). sleep_shape_matrix.json is FROZEN (spec §Test Strategy: do not
  modify). The session lane adds a PARALLEL emission — it must not alter
  the existing sleep_analysis/sleep_in_bed emissions, so the lock stays
  green untouched.

## Recommended approach (adopted into spec)

1. Migration `0003_sleep_sessions.sql`: additive-only CREATE TABLE +
   UNIQUE (source_name, sleep_start) + index on day_kst; day_kst generated
   fixed UTC+9 (same expression family as 0001 raw_samples).
2. Normalizer: new `_sleep_session_rows(parsed)` — recognizes the SAME
   sleep-shaped points as the existing branch (date + ∩ sleep fields),
   verbatim field extraction, skip+count when sleepStart unparseable.
   Pure function, no I/O (normalize.py module contract).
3. Pipeline: `_upsert_sleep_session_lane` inside the same per-payload
   transaction (ingest_payload shape), ON CONFLICT (source_name,
   sleep_start) DO UPDATE, skipped merge via merge_skipped.
4. Query layer: `list_sleep_sessions(frm, to, limit)` mirroring
   `list_ecgs` (day_kst range where, ORDER BY sleep_start DESC, limit
   clamp [1,500] default 100); MCP tool + CLI wrap it.
5. Route paging / list_workouts pagination / latest_activity_day:
   additive query-layer changes reusing _bounded_limit/_validated_offset.

## Confidence

[scanned-not-verified] for corpus counts (computed from archive files
2026-09-28; deterministic but not re-run in CI), [decision-only] for the
adoption of the lane pattern (spec §1 formalizes it).