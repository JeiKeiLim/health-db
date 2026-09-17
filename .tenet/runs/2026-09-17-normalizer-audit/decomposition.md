# Decomposition: parse everything (run 2026-09-17-normalizer-audit)

delivery_mode: autonomous · model_tier: **local** (fine-grained, single-responsibility jobs with explicit per-job acceptance criteria — a weaker executor needs a tighter plan).

Spec: `.tenet/runs/2026-09-17-normalizer-audit/spec.md` (readiness-passed).
Acceptance tests (written first, defining done): `tests/acceptance/parse_everything.py`.

## ASCII DAG

```
job-1  migration 0002 (ecgs/ecg_voltages/hrn/hrn_samples tables)
   │
job-2  sleep value rule + in-bed split + loosened recognition (normalize.py, payloads.py) ──┐
job-3  unit-conversion amendment (normalize.py constants + _qty_for_unit) ─────────────────┤
job-4  skip accounting (PayloadReport.skipped + ingest response + replay print) ───────────┤
job-5  ECG lane normalize (dataclasses + _ecg_rows, unix-epoch ts) ────────────────────────┤
job-6  HR-notification lane normalize (dataclasses + _hrn_rows, hr/hrv samples) ──────────┤
   │                                                                                       │
job-7  pipeline wiring: lane upserts + latest-wins SQL (pipeline.py + db/helpers.py) ────┤
   │        (depends: 1,5,6)                                                               │
job-8  MCP tools + queries + CLI + schema resource (queries.py, mcp_server.py, cli.py) ────┘
   │        (depends: 7 for data, 4 for skipped surface)
   │
e2e-1  INTEGRATION CHECKPOINT: run tests/acceptance/parse_everything.py + full unit + existing acceptance suites (report-only)
   │
job-9  fixtures + census lock + docs (README iron-law wording, backfill-runbook ordering note, journal doctrine-drift note)
   │
e2e-2  FINAL: full verification — pytest (unit+acceptance), ruff, perf gate non-regression, repair-path manual steps documented (report-only)
```

## Job details

### job-1 — migration 0002
- **Type**: dev. **Depends on**: none.
- **Deliverable**: `migrations/0002_parse_everything.sql` — additive-only CREATE TABLE for `ecgs`, `ecg_voltages`, `heart_rate_notifications`, `heart_rate_notification_samples` with the exact columns/keys from spec §6 (day_kst generated expression copied verbatim from 0001_init.sql:50-51; UNIQUE (source_name, start_ts) on both parent tables; PKs (ecg_id, seq) and (notification_id, ts, kind)). No ALTER/DROP on existing tables.
- **Acceptance**: file exists; `test_migration_0002_additive_only` passes; applier (health_db/db/migrations.py) picks it up — unit test asserting 0002 applies to a disposable pg and re-apply is a no-op (versioned applier behavior).
- **Interface contract**: downstream jobs (5,6,7) read these exact table/column names; no job invents its own DDL.

### job-2 — sleep value rule + in-bed split + loosened recognition
- **Type**: dev. **Depends on**: none (pure functions).
- **Deliverable**: normalize.py `_rows_for_point` sleep branch per spec §1 precedence (totalSleep>0 → asleep>0 → core+deep+rem>0 → inBed split to `sleep_in_bed` → skip counted); `DISPLAY_NAMES["sleep_in_bed"]="Sleep In-Bed"`; payloads.py `_SLEEP_AGGREGATED_KEYS` loosened to the sufficient-key-set rule from spec §1 (unaggregated/common precedence preserved).
- **Acceptance**: unit tests — WATCH_NIGHT fixture → 6.3746…; MIXED_STAGE_NIGHT → 8.2687…; AUTOSLEEP_NAP → 0.5833…; INBED_ONLY → one sleep_in_bed row qty 7.3322, zero sleep_analysis rows; NO_TOTALSLEEP_SHAPE → 7.0; all-zero point → zero rows + counted skip. Existing sleep unit tests updated (asleep-wins-when-present stays for the no-totalSleep shape; the asleep-preference test with totalSleep=7.5+asleep=6.8 must be re-derived: totalSleep wins → 7.5).
- **Interface contract**: emits SampleRow with name `sleep_in_bed` (unit hr) — pipeline (job-7) needs no change since metric_dim auto-registers; skip counts flow to job-4's accounting.

### job-3 — unit-conversion amendment
- **Type**: dev. **Depends on**: none (pure functions).
- **Deliverable**: named constants `LB_PER_KG = 2.20462`, `ST_PER_KG = 0.157473` next to KJ_PER_KCAL; `_qty_for_unit` converts lb/st→kg, °F→°C (wrist temperature only) with exact factors, per spec §4; unconvertible units skip + count (job-4 hook: return a reason, not just None).
- **Acceptance**: unit tests — 160 lb → 72.57 kg; 11.8 st → 74.93 kg; 95.5 °F → 35.2777 °C; kJ→kcal unchanged; a fabricated 'gal' weight row → skipped with a visible count (via job-4's mechanism once wired).
- **Interface contract**: `_qty_for_unit` signature may gain a skip-reason out-param or the skip decision moves to a small dataclass — job-4 consumes it; keep the pure-function contract (no I/O).

### job-4 — skip accounting
- **Type**: dev. **Depends on**: job-2, job-3 (their skip reasons feed the counters).
- **Deliverable**: `PayloadReport.skipped: dict[str,int]` with keys `unit_unconvertible`, `unrecognized_shape`, `ecg`, `notification`, `sleep_no_value` (spec §5); /api/ingest 200 body gains `"skipped"` (both pool and archive-only paths, main.py:242-255); replay CLI prints it to stderr.
- **Acceptance**: unit tests for the counting; the response shape asserted in parse_everything.py `test_no_silent_drops_in_response` + `test_unconvertible_unit_counted_visibly`.
- **Interface contract**: the dict is plain JSON; counters never block ingest (informational).

### job-5 — ECG lane normalizer
- **Type**: dev. **Depends on**: none (pure).
- **Deliverable**: `EcgRow`/`EcgVoltageRow` dataclasses (normalize.py); `_ecg_rows(document)` parsing `data.ecg` per spec §2: `_parse_ts` for start/end; voltage ts via `datetime.fromtimestamp(date, tz=UTC)` (UNIX epoch — verified 42/42; NOT CF-absolute); source name from `source` string; skip unparseable/empty per-row.
- **Acceptance**: unit tests using the ECG_SESSION fixture (3 voltages, ts 2025-10-12 01:45:06.86 UTC first) + a malformed-session skip test.
- **Interface contract**: rows consumed by job-7's upserts into job-1's tables.

### job-6 — HR-notification lane normalizer
- **Type**: dev. **Depends on**: none (pure).
- **Deliverable**: `HrnRow`/`HrnSampleRow` dataclasses; `_hrn_rows(document)` parsing `data.heartRateNotifications` per spec §3 (HR value key `heartRate`/`hrUnits`, HRV value key `hrv`/`units`, sample ts = timestamp.start; kinds 'hr'/'hrv').
- **Acceptance**: unit tests using HR_NOTIFICATION fixture (1 event, 1 hr + 1 hrv sample) + skip test.
- **Interface contract**: same as job-5.

### job-7 — pipeline wiring
- **Type**: dev. **Depends on**: job-1, job-5, job-6.
- **Deliverable**: db/helpers.py UPSERT_ECG / UPSERT_ECG_VOLTAGE / UPSERT_HRN / UPSERT_HRN_SAMPLE (parameterized SQL, latest-wins ON CONFLICT per spec §6 keys, voltage batch ≤10k); pipeline.ingest_payload persists both lanes in the same transaction (after samples, before raw_ingest); metrics/workouts lanes untouched.
- **Acceptance**: acceptance tests TestEcgLane + TestNotificationLane pass against disposable pg; re-post idempotency proven (both classes).
- **Interface contract**: one transaction per payload; rollup recompute untouched (new lanes OUT of rollups).

### job-8 — MCP tools + queries + CLI
- **Type**: dev. **Depends on**: job-7 (tables wired) — may start interfaces against job-1 tables, but full pass needs 7.
- **Deliverable**: queries.py `list_ecgs`, `get_ecg_detail` (paged voltages, default 1000/max 10000), `list_heart_notifications` (default 50/max 500, nested samples); mcp_server.py tool registrations (same auth/pagination conventions); cli.py `ecgs` / `ecg-detail` / `hrn`; schema_resource includes new tables.
- **Acceptance**: parse_everything.py TestMcpTools passes; limits enforced (400-style actionable errors beyond max, matching existing tool conventions); 90-day perf gate untouched (e2e-2 re-runs slice_2_perf).
- **Interface contract**: MCP surface additions only; no changes to existing 8 tools' schemas.

### e2e-1 — integration checkpoint
- **Type**: integration_test, report_only. **Depends on**: jobs 1-8.
- **Prompt**: Run `uv run pytest tests/acceptance/parse_everything.py -v` and `uv run pytest` (unit) against docker. Report pass/fail per test class with failure details. DO NOT fix code — report only; blocking findings via tenet_report_blocking_finding.

### job-9 — fixtures, census lock, docs
- **Type**: dev. **Depends on**: e2e-1.
- **Deliverable**: sanitized fixture files under tests/fixtures (sleep-shape matrix from real corpora; trimmed ECG/notification fixtures preserving census counts per readiness note); census-lock unit test (recent sample → 137 kept + 1 in-bed; backfill sleep fixture → 2,200 + 520 — numbers from research/predecessor-inventory.md §7); README iron-law-4 wording + backfill ordering warning (deploy BEFORE backfill); journal doctrine-drift note for the iron-law amendment.
- **Acceptance**: census test passes; ruff clean; README diff reviewed in eval.

### e2e-2 — final verification
- **Type**: integration_test, report_only. **Depends on**: job-9.
- **Prompt**: Full gate: `uv run pytest` (unit + acceptance incl. parse_everything + existing slice_1/slice_2 suites), `uv run ruff check src/ tests/`, `uv run pytest tests/acceptance/slice_2_perf.py -m requires_pg` (perf non-regression). Report everything; document the two manual server steps (redeploy + force-replay + MCP verify 6.21/6.34/6.37) as the deployment handoff section.

## Interface contracts summary

- Pure normalizers (jobs 2,3,5,6) emit frozen dataclasses — no I/O, no DB imports (module purity contract).
- Job-7 owns ALL SQL; jobs 5/6 never touch SQL (architecture rule: SQL lives in db/).
- `skipped` dict keys are frozen by spec §5 — jobs 3/5/6 increment via job-4's counters, never invent keys.
- Migration columns are frozen by spec §6 — jobs 5/6/7 read the same names.
- Acceptance tests are the shared definition of done: no job edits tests/acceptance/parse_everything.py except job-9 (fixture/census additions only — assertions stay).