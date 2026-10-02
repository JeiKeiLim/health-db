# Interview: health-db normalizer audit & sleep fix

Date: 2026-09-17
Mode: Standard
Rounds: 1

## Mode Selection
- Prompt shown: "Which Tenet execution mode for this normalizer investigation + fix run?" with recommendation Standard (known architecture, moderate unknowns from audit scope, medium complexity; Full too heavy, Quick too narrow).
- User response: confirmed Standard
- Selected mode: standard
- Selection basis: explicit_user_choice

## Clarity Score
- Goal: 1.0 (weight 0.4) — root cause confirmed by local repro; user named the fix surface and demanded a thorough audit
- Constraints: 1.0 (weight 0.3) — existing stack/architecture, iron laws, repair path all confirmed
- Success criteria: 0.5 (weight 0.3) — sleep fix criteria concrete; audit acceptance (how much evidence, which corpus) partially assumed
- **Total: 0.85 / 0.8 required** (to be validated by tenet_validate_clarity)

## Round 1

### Questions Asked
1. (Context, from prior session — recorded, not re-asked) Why is sleep data invisible through MCP?
   > Root cause found by investigation: sleep-aggregated branch in src/health_db/normalize.py:427-430 treats `asleep: 0` as present (`_num(0)` → 0.0, `is not None` → True) and stores qty=0.0. Real HAE REST v2 Watch payloads carry `asleep: 0` (sentinel) with totalSleep/core/deep/rem populated. Reproduced locally with the user's exact payload: both nights → qty 0.0; correct values 6.41/6.57 hr.

2. (Context, from prior session) Did we port the reference implementation differently?
   > Yes — half-lane port. predecessor had two lanes: `.hae` (normalize_metric_samples, normalizer.py:34-58; asleep-preference by KEY PRESENCE, valid because Watch segments omit the key) and JSON REST (normalize_json_metrics, normalizer.py:244-285; NO asleep-preference — qty → totalSleep fallback only). health-db is JSON-only but inherited the `.hae`-lane rule. Task-005's fix was .hae-lane-only. A 2026-09-13 journal note saw the asleep=0 symptom but misdiagnosed it as "inherited, not a porting bug" and flagged it for design review — never fixed.

### Round 1 — interactive decisions (user answered)

1. ECG lane (42 sessions, 600,000 voltage points, backfill-only): how to store?
   > **Full: metadata + voltages** — new `ecgs` session table + `ecg_voltages` child (workout_heart_rate pattern) + migration 0002.
2. High-heart-rate notifications (350 events, 1,900 sample points): how to store?
   > **Events + samples** — new `heart_rate_notifications` table + child sample rows, individually queryable.
3. 521 in-bed-only sleep rows (시계/iPhone, inBed>0, no stage data): how to parse?
   > **Separate `sleep_in_bed` metric** — qty = inBed hours, display "Sleep In-Bed"; sleep_analysis keeps only actual sleep. Nothing dropped, no zero rows.
4. lb weight row (silently skipped by iron law 4): what does "parse everything" mean here?
   > **Convert non-canonical units** — lb→kg, °F→°C etc. for metrics with canonical units. Deliberate iron-law-4 amendment; only conversions with exact factors; still never store a non-canonical unit next to canonical rows.

### Decisions Made (cumulative)
- Directive (steer f12b148d): "I want every data to be parsed. If you can see that data point, let's make that parsed too." — no visible data point may be silently dropped.
- Scope therefore = sleep value rule + sleep shape-recognition fix + 2 new lanes (ECG, HR notifications) with new tables (migration 0002) + sleep_in_bed metric + unit-conversion amendment (lb→kg, °F→°C).
- Research-first completed (predecessor-inventory.md §1-7): universal sleep rule evidence-complete; ECG/HRN only arrive via backfill (dailies/workout lanes never carry them — verified 130+76 files); conversion scope: unit-bearing metrics in CANONICAL_UNITS only (weight lb→kg ×÷2.20462, degF→degC for wrist temperature; no other non-canonical units observed in any corpus).
- This becomes a Tenet Standard-mode run: fix the sleep normalizer + parse-everything expansion.

### Remaining Ambiguities
- Audit corpus scope: RESOLVED by research — 130 real daily exports on private cloud storage (same JSON v2 format the server receives) + 20 archived server payloads + 3 pending = 450k points audited end-to-end. No live server deep-dive needed beyond spot checks already done.
- Fix rule for sleep values: RESOLVED BY EVIDENCE (research/predecessor-inventory.md § 6) — qty = totalSleep > 0, else core+deep+rem, else asleep > 0 (nap), else skip. Matches the predecessor JSON-lane sibling code and the real corpus; no user semantics decision required (to be confirmed in interview round).
- Cross-source overlap: RESOLVED BY EVIDENCE — AutoSleep REST rows are naps disjoint from Watch nights (8 co-days, all disjoint windows); per-source rows + rollup SUM are correct as designed; no supersede layer needed.
- Repair path for already-stored zero rows: `health-replay --dir data/archive --force` on the server after code fix + redeploy (README-documented repair path).
- Scope guardrails: no .hae consumption capability (out of scope), no schema changes (audit found none needed), optional: report-only visibility for silent unit skips.

### Gap resolution (clarity validation round 2 — all six validator gaps closed with evidence)

1. Quantified audit success metric: **zero-drop guarantee, measured by a corpus-replay parity harness** — for every reachable corpus file (130 dailies, 76 workout-lane, 20 archived, 3 pending, 1 backfill), `Σ normalized rows == Σ parseable data points`, with a per-census report of exactly which points are counted as parseable. Acceptance number: the audit census (52,000 backfill metric points + 420,000 recent + 166+1,600 workouts + 42 ECG sessions + 600,000 voltages + 350 notifications + 1,900 notification samples + 2,900 sleep rows) each map to a stored row or a child table row. Pass = 100% of census points stored.
2. Failure/edge handling: (a) rows that remain unconvertible (unit with no exact factor, e.g. a future 'st' stone weight) → SKIPPED but **counted and surfaced in the ingest response** (`skipped` field, visible in /api/ingest stats), never silent; (b) shapes that still don't match after loosened recognition → reported in existing `unknown_formats` (payloads.py mechanism already built); (c) malformed ECG/notification rows (missing id-parseable start or voltages) → skipped per-row, counted in response. Nothing crashes a payload; partial ingestion stays per-point.
3. Performance/scale targets: ECG backfill = 600,000 voltage rows ≈ same order as routes (19,000 exist today); execemany batching; hard target: **full full-history backfill re-ingest + force-replay < 30 min on server** (existing perf contract: 90-day series query < 1s, measured by tests/acceptance/slice_2_perf.py — must not regress; add a replay-runtime assertion ≤ 30 min for the backfill corpus locally). Memory: stream-decode payload once (300 MB JSON already loads in ~5 s), voltage insert in batches of ≤10k.
4. Deployment: server runs docker (`docker build` on host per README §Deployment). Sequence: git pull → rebuild image → `docker compose up -d` (health-migrate applies 0002 inside entrypoint, in one transaction; on failure container exits with nonzero and old schema+data remain — no manual rollback path needed since 0002 is additive CREATE TABLE only). Rollback = redeploy previous image; additive migration leaves existing tables untouched.
5. Dedup/rollup semantics for new lanes: `ecgs` PK = (id?) HAE gives no ECG id — synthesized key: sha256(source||start||end) truncated as uuid, or natural key (start_ts) UNIQUE with source. Voltages PK (ecg_id, seq idx); HR notifications PK (start_ts, kind) UNIQUE per source, samples PK (notification_id, ts). All upserts latest-wins like existing lanes. ECG/notification data is **not** part of daily_rollups (they are session/event lanes like workouts — no rollup integration).
6. Conversion factors pinned: lb→kg ÷ 2.20462; st(stone)→kg ÷ 0.157473 (observed in predecessor .hae history; kJ→kcal ÷4.184 already exists). °F→°C (°F−32)×5/9 for wrist temperature — none observed in current REST corpora, implemented only if trivially composable. Exact factors only, defined as named constants mirroring KJ_PER_KCAL.

### Timing constraint (verified live)
server DB currently holds only 2026-09-15→17 (3 days, hourly automations). The full-history backfill has NOT been ingested yet. Therefore this fix must land and be deployed BEFORE the backfill is run; the ECG/HRN lanes and conversion rules will be exercised by the backfill's first ingestion, not by repair.

## Model Tier Decision
- Prompt shown: "Which model tier executes the implementation jobs?" (frontier recommended, local option explained)
- User response: **local** (explicit override of the frontier recommendation)
- Selected model_tier: local
- Selection basis: explicit_user_choice
- Consequence: decomposition must produce a FINER-GRAINED DAG — more, smaller, single-responsibility jobs with explicit per-job acceptance criteria, per phases/04 rules for local-tier executors.

### Round 2 — residual-gap confirmations (validator round 2 notes)
1. Acceptance bar: **confirmed as census-DoD** — 100% of audited census stored; non-canonical-unit metrics stay pass-through (any declared unit accepted), so census is complete by construction. Security/privacy: unchanged from deployed baseline (Bearer auth on ingest+MCP, DB behind server firewall) — no new surfaces beyond the new MCP tools, which inherit the same Bearer auth; out of run scope otherwise.
2. Query surface: **MCP tools for new lanes in scope** — `list_ecgs`/`get_ecg_detail` (metadata + paged voltages), `list_heart_notifications` (events + samples), sleep_in_bed queryable via existing query_day/get_metric_series automatically. All within the <1s perf contract.
3. Versions: pinned by the repo as-is (Python via uv lockfile, Docker base in Dockerfile, postgres:18); no version changes in this run.
4. Model tier: **local, explicit user choice** (see Model Tier Decision above).

## Summary
Run 2026-09-17-normalizer-audit ("parse everything", Standard mode, local model tier): fix the sleep normalizer value rule (totalSleep → asleep → core+deep+rem → inBed split into separate sleep_in_bed metric; evidence-complete from ~3.4k real sleep rows) and expand coverage so no visible data point is silently dropped: (a) sleep aggregated shape recognition loosened (totalSleep not required), (b) new `sleep_in_bed` metric for in-bed-only rows, (c) new ECG lane (ecgs + ecg_voltages tables, migration 0002) with MCP tools list_ecgs/get_ecg_detail, (d) new heart-rate-notification lane (heart_rate_notifications + samples table) with list_heart_notifications, (e) non-canonical-unit conversion instead of skip (lb→kg, st→kg; kJ→kcal exists) — iron-law-4 amendment, unconvertible units skipped-but-counted visibly. Acceptance = 100% census-DoD over all audited corpora. Deploy fix BEFORE the full-history backfill (server holds only 3 days now); repair stored rows via force-replay. Unit + acceptance tests lock each shape against representative-payload fixtures. Local model tier → fine-grained single-responsibility DAG.