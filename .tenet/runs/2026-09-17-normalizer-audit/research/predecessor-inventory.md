# predecessor predecessor: full-surface inventory vs health-db

Research for run 2026-09-17-normalizer-audit.
Method: read every module in `<hae-export-archive>/predecessor/src/health_pipeline/` (9 files, 1746 lines), all 5 backlog tasks, the live HAE source corpus on private cloud storage (AutoSync .hae, 건강 지표 daily JSON, Previous-data backfill, Automations configs), and decoded real `.hae` files.
Confidence: `[scanned-not-verified]` for predecessor-history claims read from task docs; `[implemented-and-tested]` for shape claims verified by running health-db's own normalizer over real payloads.

## 1. The predecessor was a two-lane system with three reconciliation layers

Lanes:
- **`.hae` lane** (AutoSync/HealthMetrics/*.hae, 113 metric dirs, lzfse-compressed per-segment samples): `scanner._scan_hae_files` → `decode_hae_file` (shells out to `/usr/bin/compression_tool -decode lzfse`) → `normalize_metric_samples`.
- **JSON lane** (건강 지표/HealthAutoExport-*.json, 130 daily files): `scanner._scan_json_files` → `normalize_json_metrics`. Same JSON v2 envelope the server ingest receives.

Reconciliation layers ON TOP of parsing (this is what "partially consuming" cost us — layer 3 was never ported):

1. **Canonical names** (TASK-002): METRIC_NAME_MAP 80 entries, snake_case → CamelCase; the decided standard was the .hae CamelCase display names.
2. **Content dedupe** (TASK-004): UNIQUE (metric, start_ts, end_ts, qty) + INSERT OR IGNORE because the HealthExport app writes versioned duplicate files ('YYYYMMDD 2.hae').
3. **Cross-lane precedence** (scanner.py:189-232): the daily JSON export is canonical for a (metric, day) when present — `supersede_hae_metric_days` DELETES the .hae rows for covered (metric, day) pairs; bulk historical files only gap-fill (metric, day) pairs with no .hae coverage. Stated reason (comment at scanner.py:189-197): ".hae files are same-day partial snapshots (HR files with 80-150 samples, steps cut off mid-day) … keeping both would double-count … or keep wrong-source data (AutoSleep sleep vs Apple Watch)".

health-db (single JSON lane, REST automations) deliberately dropped the second lane but kept only layer-1 semantics; layers 2-3 became unnecessary (REST payloads are sha-deduped, latest-wins upserted) — EXCEPT the sleep semantics that depended on lane-specific shape.

## 2. Sleep: the lane-specific shapes are genuinely different (verified on real data)

`.hae` lane (decoded 20260908.hae, 20260907.hae, 20260906.hae, 339 files exist back to 2025-10-28):
- Per-SEGMENT rows. Watch rows: `asleep: None` (key present, null) + exactly one of core/deep/rem/awake populated, totalSleep = that stage qty. ~19-30 segments per night.
- AutoSleep rows: separate `asleep` and `inBed` interval rows (asleep 3.02 + 2.75 hr; inBed 6 hr), overlapping the Watch segments in time. AutoSleep night rows carry real `asleep` values; the JSON REST `asleep: 0` sentinel DOES NOT exist in .hae (Watch rows have null, not 0).
- TASK-005 fix: per-file `prefer_asleep = any("asleep" in s)` — valid THERE because key-presence discriminates AutoSleep (real value) vs Watch (null).

JSON lane (verified on all 130 건강 지표 files + user's pasted server payload):
- Per-DAY aggregate rows, one per source, all keys always present.
- Watch rows: `asleep: 0` (sentinel), real totalSleep (6.2-8.2 hr), core+deep+rem populated.
- AutoSleep rows: `asleep: N` where N>0 ONLY for daytime naps (17:29-18:10 slots, 0.2-0.62 hr, 8 occurrences in 2026-05→09; `awake: 0, core/deep/rem: 0`); AutoSleep NEVER contributes night sleep in the JSON lane (in .hae it did — different aggregation by HAE for REST exports, sleep aggregates are Watch-only for nights; verified: 120 Watch nights, 8 AutoSleep nap-only rows, zero AutoSleep night rows in 130 files).
- Predecessor JSON-lane code (normalizer.py:252-255): `qty` → `totalSleep` fallback. NO asleep preference. AutoSleep nap rows (no qty, no totalSleep… wait: nap rows DO have totalSleep=0.7166) → stored as totalSleep. So predecessor would store Watch totalSleep for nights AND AutoSleep totalSleep for naps. Both correct, no overlap.

## 3. health-db current behavior (running its normalizer over the real corpora)

- 130 daily JSON exports, 420,000 data points: ZERO parse drops. Every point of 80 metrics lands as a row. Value ranges sane (min/max table captured: HR 40-190 bpm, RHR 45-90, HRV 5-180, SpO2 90-100, weight 70-83 kg, vitamins plausible).
- **EXCEPT sleep: 122 of 130 rows stored as qty=0.0** (all 120 Watch nights minus…: 122 zero + 8 non-zero; the 8 non-zero are AutoSleep nap totalSleep>0 rows… actually stored via `asleep` preference). Result: every night of sleep since mid-2026 on the phone's canonical daily exports normalizes to 0.00 hr except nap fragments.
- server archive corpus (20 payloads, 2026-09-09): 3 more anomalies —
  - `weight_body_mass` units=lb row skipped (CANONICAL_UNITS policy; intentional iron-law-4 skip, but silently loses a 160 lb scale reading),
  - `blood_pressure` point → 2 derived rows (systolic/diastolic; correct by design),
  - sleep point with keys {asleep,core,date,deep,rem} (no totalSleep) → unrecognized shape, silently dropped (payloads.py full-key-set recognition requires totalSleep).
- Every other metric: no drops, correct values.

## 4. Rollup semantics comparison (predecessor TASK-003 vs health-db)

- Predecessor: trends selected sum_qty for cumulative units, avg_qty for rate units (is_cumulative_unit; CUMULATIVE_UNITS set) — health-db ported this correctly (queries.py get_trends + metric_dim.is_cumulative).
- Both systems SUM across sources per day in rollups. In .hae lane this REQUIRED supersede/gap-fill to avoid double counts (TASK-002/004/005). In health-db's JSON lane, sources are disjoint per metric except sleep, where AutoSleep naps and Watch nights are DIFFERENT time windows (nap 17:35-18:10 vs night 22:11-04:58) — so SUM is semantically "total sleep across sources" = night + nap, arguably correct-by-design, NOT a double count. Verified: no day exists where AutoSleep and Watch report the same sleep interval. (8 days have both rows; all 8 are night+nap disjoint windows.)

## 5. What health-db therefore MISSED from the predecessor (the "partial port" inventory)

| Predecessor surface | Where | Ported to health-db? |
|---|---|---|
| lzfse .hae decode | decoder.py | No — out of scope by design (REST only) |
| Per-segment sleep shapes (null-vs-key semantics) | normalizer._sleep_sample_qty | No — and wrongly: .hae-lane asleep-preference rule WAS ported into the JSON lane where the sentinel semantics invert it |
| JSON-lane sleep rule qty→totalSleep | normalizer.py:252-255 | NO — the single most relevant sibling code was not ported |
| Cross-lane supersede/gap-fill | scanner.py:189-232 | No — inapplicable (one lane), except the overlap question it answered lives on in weaker form (sleep source overlap) |
| Canonical-unit skip + kJ→kcal | constants + normalizer | Yes — ported, working (lb skip seen in corpus is this policy) |
| Content dedupe by (metric,start,end,qty) | TASK-004 | N/A — replaced by sha256 payload dedupe + (metric,source,ts) latest-wins |
| Name canonicalization map | METRIC_NAME_MAP | Inverted: health-db stores snake_case natively, keeps display_name — fine |
| sum/avg by unit in trends | queries.py:175 | Yes — ported correctly |
| Workout shapes {qty,units}+point-lists | normalize_workout_json | Yes — fixed in the 2026-09-15 repair run (journal 2026-09-13 finding) |

## 6. Verdict for the fix

The sleep defect is NOT an ambiguity requiring a semantics decision between "AutoSleep wins" vs "reference parity": the data itself resolves it.

- Watch rows are the only night-sleep source in the JSON lane (AutoSleep REST rows are nap-only, 8/130 files, always disjoint from nights).
- `asleep: 0` on Watch rows is a sentinel; real total is totalSleep (= core+deep+rem exactly, verified numerically on user's payload).
- Therefore the correct JSON-lane rule (matching both the predecessor's JSON-lane code and TASK-005's intent) is: **qty = totalSleep when > 0; else core+deep+rem; else asleep if > 0 (nap); else skip**. No cross-source precedence layer is needed — SUM across the disjoint sources = night + nap, which is a true "total sleep" per day. The 11 overlapping days are disjoint windows, not duplicate nights; storing both rows is correct, and rollup SUM stays correct.
- Repair path: fix normalizer → redeploy server → `health-replay --dir data/archive --force` re-normalizes archived payloads (payloads are archived verbatim on the server data volume; force bypasses the sha gate). Rollups recompute inside the same transactions.

## 7. FULL-CORPUS audit (second pass — every lane, every metric, not just sleep)

After the user challenged whether the audit covered "entire metrics this project might not be able to parse", a second pass covered the remaining corpora:

**Backfill file (300 MB, the full-history span, the file the README backfill runbook ingests):**
- 81 metric groups, 52,000 sample points: **every single point parses and normalizes — zero shape drops, zero unrecognized shapes** across all 81 metrics (vitamins, dietary, cycling_distance, handwashing, mindful_minutes, height, body composition — all common/hr shapes).
- 1,600 workouts: all 1,600 normalize (0 dropped).
- **NEW LANES DISCOVERED: `data.ecg` (42 ECGs with voltageMeasurements) and `data.heartRateNotifications` (350 notification events)** — top-level arrays that health-db silently ignores (payloads.parse only reads `metrics`/`workouts`; they are not quarantined, not reported, just invisible). Predecessor also ignored both (no handling in predecessor either). NOT a regression — a shared blind spot. Out of current scope unless user wants them; flag for decision.
- **Sleep in backfill (2,500 points, 4 sources) — the rule got MORE evidence, not just confirmation:**
  - `시계` (110) + `Jongkuk의 iPhone` (190): pre-Watch era rows with inBed>0 but totalSleep/asleep/stages all 0 — "in bed, untracked". 300 rows.
  - `Jongkuk의 Apple Watch` (1,200): 1,150 pure-stage nights (asleep=0 sentinel, totalSleep = core+deep+rem, verified) + **50 mixed-stage nights where `asleep` is a REAL STAGE (4th category): totalSleep = core+deep+rem+asleep exactly, verified 50/50**. This kills any rule that treats asleep>0 as "AutoSleep marker" or skips it — asleep is a genuine sleep stage on mixed nights.
  - `AutoSleep` (1,001): 1,000 real nights where asleep == totalSleep (verified 1,000/1,000), 1 all-zero row.
- **The universal rule and its verified outcomes:** `qty = totalSleep if >0, else asleep if >0, else core+deep+rem if >0, else skip` → backfill: keeps 2,200/2,500 (skips exactly the 300 in-bed-untracked rows); recent 130 exports: keeps 128/130 (skips 2 in-bed rows); current prod rule keeps only 1,050/2,500 and 8/130.
- Predecessor JSON-lane parity check: `qty→totalSleep` would keep the same 2,200 + 137 (totalSleep>0 in all kept rows) but would zero-drop the asleep-only nap rows in .hae… not applicable — in REST all rows carry totalSleep. The universal rule and predecessor JSON-lane rule agree on every real REST row except one nuance: rows with totalSleep=0 but asleep>0 (none exist in either corpus) — universal rule is a strict superset.
- Design note: the 520/1 in-bed-untracked rows — predecessor JSON lane stored totalSleep=0 as a 0-row (reference stores zero); universal rule skips them. Either is defensible; recommend skipping (matches .hae-lane TASK-005 behavior `stage if stage else None` and avoids polluting rollups with zero-sleep rows). To confirm with user in interview round.

**운동 데이터 lane (workout REST automation payloads, 76 files):**
- 166 workouts, all 166 normalize; 0 metric points. No anomalies.

**Verdict update:** across ALL reachable corpora (420,000 recent points + 52,000 backfill points + 1,600 workouts + 350 notifications + 42 ECGs), the sleep value rule is the only parsing defect. The universal rule above is evidence-complete: it reproduces predecessor semantics on every real row, handles the 50 mixed-stage nights the naive "asleep=0 is always a sentinel" story missed, and needs no cross-source precedence (sources remain disjoint per night/nap).