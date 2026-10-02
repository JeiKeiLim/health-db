# Scenarios: 2026-09-28-mcp-analysis-parity

## Scenarios (Success)

1. **Morning brief reads stages from MCP.** The consumer calls `list_sleep_sessions(from="2026-09-28", to="2026-09-28")` and gets the night ending 9/28: total_sleep 6.2614, deep 1.0430, core 3.7234, rem 1.4950, awake 0.2620, sleep_start 2026-09-27 23:05:35 +0900, sleep_end 2026-09-28 05:49:54 +0900, source Apple Watch. No legacy file read. The brief says "deep 0.87 / REM 1.67" sourced from MCP.

2. **Nap and night co-exist unmerged.** Day 2026-09-17 carries a Watch night (start 22:11:12, total 6.3746) AND an AutoSleep nap (start 17:35:00, total 0.5833, asleep 0.5833, in_bed 0.5833). `list_sleep_sessions(from="2026-09-17", to="2026-09-17")` returns BOTH rows — distinct sources, distinct sleep_start, never merged. The consumer applies its own 16:00–18:59 nap exclusion client-side.

3. **Backfill populates the full lane.** After deploy, `docker exec health-db health-replay --dir data/archive --force` walks the archived payloads and stores one session row per sleep-shaped point (130 recent-daily + 2,500 backfill corpus). Running the replay a second time changes nothing (row counts identical, zero duplicates) — latest-wins ON CONFLICT (source_name, sleep_start).

4. **Sentinel stored verbatim, never "fixed".** A Watch night with `asleep: 0` stores asleep = 0.0 in sleep_sessions (verbatim), while sleep_analysis still shows totalSleep 6.2614 (existing precedence untouched). A mixed-stage night with real `asleep: 2.05` stores 2.05 — no re-derivation either way.

5. **Route paging kills the context flood.** `get_workout_detail("e5a1b2c3-4d5e-4f60-8a9b-0c1d2e3f4a5b")` returns route_count 2873 with a default first page of 1000 points and `next_route_offset: 1000` — no more ~1 MB inline dump. Walking 4 pages (offsets 0/1000/2000/3000) yields 1000/1000/1000/99 points and `next_route_offset: null` on the last; summary fields (duration 2864.40 s, avg_hr 137.85, distance 6.824 km) identical on every page.

6. **Activity-only freshness marker.** With nutrition metrics ≥30 days stale, `freshness_check()` returns `latest_activity_day: "2026-09-28"` (max over the 7 activity metrics) while `latest_complete_day` keeps its median-consensus semantics — the consumer can date-label reports without client-side derivation, and cron scripts stop mislabeling stale-nutrition days.

7. **Full-year workout listing without silent truncation.** `list_workouts(from="2018-01-01", to="2018-12-31", limit=100)` returns page 1 of 100 rows with `next_offset: 100`; walking to the end yields pages 100/100/40, `next_offset: null`, 240 total — previously LIMIT 1000 silently truncated nothing visible, but ranges over 1000 rows were invisible.

8. **Probe metrics vanish.** After the cleanup SQL, `list_metrics` returns 83 metrics (was 85); `__probe_metric`/`__probe_metric2` absent from `freshness_check` per-metric listing.

9. **No sleepStart → visible skip.** A sleep-shaped point lacking a parseable `sleepStart` (the `no_totalsleep_shape` matrix shape) produces NO session row and increments `skipped.sleep_session` in the ingest response — zero silent drops.

## Anti-Scenarios (Failure)

1. **Existing emission drift.** Any change to `sleep_analysis`/`sleep_in_bed` rows emitted today — counts (128+2 recent / 2,200+300 backfill census) or stored qty (sentinel nights must keep totalSleep, never 0.0) — breaks the census lock. The session lane must be purely parallel.

2. **Merged sessions.** Collapsing the AutoSleep nap into the Watch night, or deduplicating same-day records from one source into a single row — violates never-merge. Two records with different (source, sleep_start) are two rows, always.

3. **Sentinel re-derivation.** "Fixing" `asleep: 0` by computing it from totalSleep − stages, or zero-filling missing stages, or filtering naps at the server — the lane stores payload truth verbatim or fails the spec.

4. **Silent session drop.** A sleep-shaped point yielding neither a session row nor a `skipped.sleep_session` count — regression of the defect class this project exists to kill (zero-silent-drops contract).

5. **Migration damage.** 0003 altering existing tables/columns, or leaving partial state on failure (must be one transaction; additive-only).

6. **Rollup contamination.** sleep_sessions rows leaking into daily_rollups, or the 90-day series perf gate regressing past 1 s.

7. **Response-shape break.** Removing/renaming existing fields in get_workout_detail/list_workouts/freshness_check responses — consumers depend on them; changes are additive only (route_count, next_route_offset, latest_activity_day, limit/offset/next_offset).

8. **Cleanup before replay.** Probe-metric SQL running BEFORE the backfill replay lets replay re-emit probe rows from archived payloads — cleanup must be ordered after replay (deploy sequence step 3).