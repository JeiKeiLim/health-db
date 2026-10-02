# Scenarios: 2026-09-17-normalizer-audit

## Scenarios (Success)

1. **Watch night stored correctly.** A daily export carrying `{"date": "2026-09-17 00:00:00 +0900", "source": "Jongkuk의 Apple Watch", "asleep": 0, "totalSleep": 6.37, "core": 3.58, "deep": 1.05, "rem": 1.74, "inBed": 0, ...}` posts to /api/ingest. `query_day("2026-09-17", "sleep_analysis")` returns sum 6.37 hr (not 0.0), and the Watch source row has qty 6.37. The user asks an agent "how did I sleep last night?" and gets the true value.

2. **Mixed-stage night (50-case).** A Watch row with `asleep: 2.05, core: 3.075, deep: 1.208, rem: 1.9375, totalSleep: 8.2` (real 2024-10-09 backfill shape) normalizes to qty 8.367 — totalSleep wins, the real asleep stage does not override it, and the sum is not core+deep+rem (which would lose the 2.05 hr).

3. **AutoSleep nap co-existing with a Watch night.** A day carrying both a Watch night (`totalSleep` 6.37, sleepStart 22:11) and an AutoSleep nap (`asleep` 0.58, sleepStart 17:35) stores BOTH rows; `query_day` returns sum 6.95 (= night + nap), each source's row individually visible in `raw_samples`.

4. **In-bed-only era row becomes sleep_in_bed.** A 2018 `시계` row `{"inBed": 7.33, "totalSleep": 0, "asleep": 0, stages: 0, sleepStart: "2018-04-02 00:15:08 +0900"}` produces a `sleep_in_bed` row qty 7.33 (display "Sleep In-Bed") and NO sleep_analysis row; `query_day("2018-04-02", "sleep_in_bed")` returns 7.33.

5. **ECG backfill ingestion.** The full-history backfill POSTs; 46 `ecgs` sessions land with classification ("정상 동율 리듬" ×38, etc.), avg HR, and 600,000 `ecg_voltages` rows with correct UNIX-epoch timestamps (first voltage == session start ±2s, verified 42/42 — never year-2056 CF-absolute values); `list_ecgs` lists them, `get_ecg_detail` pages 1,000 voltages at a time; re-posting the same file stores zero new rows.

6. **High-heart-rate notifications.** 350 events land with kind/threshold/window; the 1,900 notification samples (HR + HRV) attach via (notification_id, ts, kind); `list_heart_notifications(from, to)` returns events with nested samples.

7. **lb converts visibly.** The archived `weight_body_mass` lb payload (160 lb) stores as 72.57 kg under unit kg; the ingest response reports `skipped: {"unit_unconvertible": 0, ...}`; a hypothetical future unit with no factor appears there with a count instead of vanishing.

8. **Repair of live days.** After redeploy, `health-replay --dir data/archive --force` rewrites 2026-09-15/16/17 sleep rows from 0.0 to 6.21/6.34/6.37 and rollups match; `get_trends("sleep_analysis")` over the window shows real values.

9. **Recognition loosened.** A sleep point `{date, asleep: 7.0, core, deep, rem}` without totalSleep (the 2026-09-08 archive corpus shape) normalizes instead of being dropped.

## Anti-Scenarios (Failure)

1. **Silent drop.** Any data point visible in a payload that produces neither a stored row nor a `skipped` entry in the ingest response — the exact defect class this run exists to kill. Zero tolerance.

2. **Sentinel wins again.** A Watch night storing qty 0.0 because `asleep: 0` was treated as a value — regression of the root bug. Fixture-locked.

3. **Mixed-stage undercount.** The 49 mixed nights storing core+deep+rem (6.2188) instead of totalSleep (8.2) — losing the asleep stage — must not happen; totalSleep has absolute precedence when > 0.

4. **In-bed pollution.** In-bed hours landing in `sleep_analysis` as zero rows or inflating night totals — sleep_analysis must contain only actual sleep.

5. **Rollup contamination.** ECG/notification rows leaking into `daily_rollups` (they are session lanes) or the 90-day series perf gate regressing past 1s after 600k voltage inserts.

6. **Migration damage.** 0002 touching existing tables/columns, or a failed migration leaving partial state (must be one transaction; additive-only).

7. **Fixture/census drift.** The census lock test failing silently skipped — rule changes must update the census comment and fixtures together, never leave them out of sync.

8. **Backfill before fix.** The user runs the full-history backfill BEFORE this run's deploy — ECG/notifications lost again, sleep 0.0 for the whole span. Sequencing is part of the deliverable (documented in spec §8; surfaced at pre-execution confirmation).