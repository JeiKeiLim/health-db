# Interview: health-db MCP analysis parity

Date: 2026-09-28
Mode: Full
Rounds: 2

## Mode Selection
- Prompt shown: "Which Tenet execution mode? Full recommended — P1 cutover-blocker feature with a new migration, new MCP tool, and 5 changes total; consumer-verified spec; but mirrors existing ecgs/heart_rate_notifications lane patterns."
- User response: confirmed "Full" (and earlier: "make it as a proper tenet run as full tenet process")
- Selected mode: full
- Selection basis: explicit_user_choice

## Clarity Score
- Round 1 validator result: Goal 0.9, Constraints 0.7, Success criteria 0.7 → 0.78 (below 0.8 gate). Gaps resolved in Round 2 — see gap resolutions there.
- Round 2 validator result: Goal 0.85, Constraints 0.8, Success criteria 0.85 → **0.835 PASSED** (0.8 gate).
- Advisory gaps carried into spec phase (non-blocking): migration 0003 rollback/down path should be documented in the spec (house convention: migrations are forward-only additive; `health-migrate` applies at startup; failure = container entrypoint aborts, no partial state since each migration runs in a transaction); `health sleep` CLI output format should be pinned in spec (JSON to stdout, diagnostics to stderr — house convention); latest-wins upsert concurrency (single-writer HAE ingest via pipeline transaction; no parallel writers exist); route/list_workouts paging latency (covered by existing <1s rollup perf contract — sessions/paging queries are indexed day_kst/UUID lookups).

## Round 1

### Questions Asked
1. Scope confirmation: implement all 5 changes (sleep_sessions lane P1, route paging P2, latest_activity_day P2, workout pagination P3, probe cleanup P3) in this run?
   > "All 5 as spec'd (Recommended)"

2. Upsert key design — row identity for sleep_sessions: keep spec's UNIQUE (source_name, sleep_start), or switch to one-row-per-night?
   > "I see I thought you were referring to .hae file but HAE stands for Health Auto Export app correct? then it's fine. and I choose option 1" — keep (source_name, sleep_start). (User initially confused "HAE" with the legacy .hae file lane; clarified HAE = Health Auto Export iOS app, the REST JSON producer in this project.)

3. Sequencing: one autonomous run with single deploy, or split deployments?
   > "Single run (Recommended)" — P1→P2→P3 in dependency order; probe cleanup runs after the backfill replay so replay cannot resurrect probe rows before cleanup.

4. Data semantics: store payload values verbatim with no re-derivation, per spec?
   > "Verbatim (Recommended)" — asleep:0 stored as-is (known Watch sentinel), stages never re-derived, naps/dozes never filtered, per-source rows never merged.

### Decisions Made
- Run identity: slug `2026-09-28-mcp-analysis-parity`, run path `.tenet/runs/2026-09-28-mcp-analysis-parity/`
- Spec source: `specs/2026-09-28-mcp-analysis-parity.md` (consumer-written by Max, the Hermes health agent — primary MCP consumer), status proposed; adopted as the run spec basis, to be finalized into the run's spec artifact
- Scope: all 5 changes, single run, single deploy
- Row identity: UNIQUE (source_name, sleep_start), latest-wins upsert — same semantics family as raw_samples (metric_id, source_id, ts)
- Data semantics: verbatim storage, zero-silent-drops (rows lacking parseable sleep_start skipped + counted under new additive `skipped.sleep_session` key)
- Probe cleanup ordering: after `health-replay --dir data/archive --force` backfill (replay would otherwise re-emit probe rows before cleanup)
- The consumer spec was independently verified by the orchestrator against repo HEAD `dd668f8`: normalizer sleep branch (normalize.py:635-676) drops sleepStart/End + inBedStart/End + deep/core/rem/awake today; get_workout_detail (queries.py:550-558) returns all route points inline (verified ~2,873 pts ≈ 1 MB); list_workouts hardcodes LIMIT 1000 (queries.py:484); freshness_check has latest_complete_day only (queries.py:189+). All spec "current behavior" claims check out.
- Sleep archive corpus verified: 2,500 backfill nights, 100% carry sleepStart/End stamps, 1,200 with real stages, 1,140 with awake>0. Backfill via replay-from-archive is free (no phone re-export needed).

### Remaining Ambiguities
- None material. Spec §6 observations (HR source "unknown", HR density, nutrition staleness) are confirm-only items — confirmed as intended/expected by the orchestrator's verification; no code change requested by the consumer.

## Round 2 (clarity-gap resolution)

The first clarity validation scored 0.78 (below 0.8) with 7 gaps. Six were "transcript didn't restate what the spec already documents" — resolved below by recording the concrete values. One (delivery-mode prompt wording) is procedural; the agile alternative was in fact presented in the option list, and the user chose autonomously from both options shown.

### Questions Asked
5. Failure-mode behavior for the new paginated surfaces: follow existing house convention?
   > "House convention (Recommended)" — empty result → empty `sessions` array (never an error); paging past last page → empty array + `next_offset`/`next_route_offset: null`; invalid limit/offset clamped silently. Identical to `raw_samples` / `list_workouts` / `get_ecg_detail` conventions.

6. Security surface: the new sleep tool and schema entry inherit existing Bearer auth unchanged?
   > "Inherit Bearer (Recommended)" — no new auth surface, no new secrets, no PII beyond what MCP already serves (sleep stages join HR/HRV/sleep totals already exposed). `health://schema` gains a table description only.

### Gap resolutions (recorded for the clarity validator; all values from the adopted spec)
- **Success criteria 1–9 are concrete and testable** (spec §Success Criteria): e.g. (1) `list_sleep_sessions` for night ending 2026-09-28 returns total 6.2614, deep 1.0430, core 3.7234, rem 1.4950, awake 0.2620, start 2026-09-27 23:05:35 +0900, end 05:49:54 +0900; AutoSleep nap day 2026-09-17: total 0.5833, start 17:35:00. (2) Double `health-replay --dir data/archive --force` leaves row counts unchanged; census = one row per sleep-shaped payload point (130 recent-daily + 2,500 backfill); only permitted drop = no parseable sleep_start, counted under `skipped.sleep_session`. (3) Census lock test passes untouched; night totals 9/27=7.6521h, 9/28=6.2614h unchanged. (4) Route paging on workout e5a1b2c3-…: route_count 2873, pages 1000/1000/1000/99, next_route_offset 1000/2000/3000/None; summary fields identical (duration 2864.40s, avg_hr 137.85, active_energy 496.74 kcal, distance 6.824 km). (5) `latest_activity_day` == 2026-09-28 with nutrition ≥30d stale. (6) `list_workouts` 2018 range: pages 100/100/40, total 240, next_offset 100/200/None. (7) Probe cleanup: metrics count 85→83. (8) Full suite green + ruff clean; 90-day series <1s. (9) Live server verification incl. backfill-era spot check (night ending 2026-08-20 = 5.9714h).
- **Failure scenarios defined**: house paging convention (Q5 above); unparseable sleep_start → skip + count under `skipped.sleep_session` (zero-silent-drops); unknown workout UUID → existing `UnknownWorkout` actionable error (unchanged); missing sleep_end/in-bed fields → NULL columns (not errors).
- **Quantified budgets**: route paging default route_limit=1000, clamp [1,10000] — kills the ~1.0MB inline flood (2,873 pts verified live). `list_sleep_sessions` limit default 100, clamp [1,500]. `list_workouts` limit default 500, clamp [1,2000]. Existing perf contract (90-day series <1s) unchanged.
- **Security confirmed by user** (Q6): Bearer-only, inherited; no new secrets; single-user threat model.
- **Census lock gate**: `tests/unit/test_census_lock.py` — asserts every shape in `tests/fixtures/parse_everything/sleep_shape_matrix.json` (frozen, must not be modified) normalizes to the locked expected rows; the sleep_sessions lane adds emission WITHOUT altering existing sleep_analysis/sleep_in_bed rows, so the test must pass unchanged (success criterion 3).
- **Tech stack constraints explicitly confirmed by user**: scope Q1 + semantics Q4 + sequencing Q3 constitute explicit user confirmation of the operational constraints; stack itself is project doctrine (Python 3.12+/uv, psycopg3, FastAPI, mcp SDK v2, PostgreSQL 18, pytest+ruff, Docker on self-hosted) — no new dependencies permitted (spec §Tech Stack).
- **Delivery-mode procedural note**: the interactive prompt presented both autonomous AND agile as options with descriptions ("Sliced delivery: plan-checkpoint, use-checkpoints after each slice"); user selected autonomous from the full list.

### Decisions Made
- Edge behavior: house convention (Q5)
- Security: inherit Bearer, no new surface (Q6)

### Remaining Ambiguities
- None. All clarity-g validator gaps resolved with user confirmation or concrete spec values recorded above.

## Delivery Mode Decision
- Prompt shown: "Delivery mode (Full-mode hard gate): how should this run be sliced? The spec is fully crystallized with exact values and verified claims, and you've already confirmed scope, semantics, and sequencing — autonomous can run it end-to-end after your final pre-execution confirmation."
- User response: "Autonomous (Recommended)"
- Selected delivery_mode: autonomous
- Selection basis: explicit_user_choice

## Model Tier Decision
- Prompt shown: "Model tier: which tier of model executes the implementation jobs? (Advisory — shapes decomposition granularity only; default frontier.)"
- User response: "Local"
- Selected model_tier: local
- Selection basis: explicit_user_choice
- Implication: decomposition MUST produce a finer-grained DAG — more, smaller, single-responsibility jobs with explicit per-job acceptance criteria, per phases/04-decomposition.md local-tier guidance.

## Summary
Full-mode autonomous run for the MCP analysis-parity spec (`specs/2026-09-28-mcp-analysis-parity.md`): a new `sleep_sessions` session lane (migration 0003 + normalizer emission + pipeline store + query layer + MCP tool `list_sleep_sessions` + CLI `health sleep` + schema resource entry), plus three additive query-surface changes (get_workout_detail route paging mirroring get_ecg_detail, freshness latest_activity_day, list_workouts pagination) and one ops cleanup (probe-metric SQL). Verbatim payload semantics, never-merge rows, latest-wins idempotency, existing sleep_analysis/sleep_in_bed emissions untouched, census lock must stay green. Post-deploy: replay backfill (run twice for idempotency), probe cleanup, live MCP verification of success criteria 1–9, README/schema updates. Local model tier → fine-grained job DAG. Consumer-side legacy-pipeline retirement follows from the consumer once criteria pass.