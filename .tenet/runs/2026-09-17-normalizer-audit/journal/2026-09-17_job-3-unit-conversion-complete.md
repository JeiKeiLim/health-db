# job-3 complete: unit-conversion amendment (lb/st/degF) + visible skips

Commit: 25f7a8a `tenet(job-3): unit-conversion amendment (lb/st/degF convert, unconvertible counted visibly)`

## Deliverable (spec §4, iron-law-4 amendment)

- `LB_PER_KG = 2.20462`, `ST_PER_KG = 0.157473` named constants next to `KJ_PER_KCAL` (normalize.py).
- `_qty_for_unit` now returns a frozen `UnitDecision(row, skip_reason)` instead of a bare tuple|None:
  - canonical match → row unchanged; pass-through metrics (not in CANONICAL_UNITS) unchanged.
  - lb→kg ÷ 2.20462 and st→kg ÷ 0.157473 for `weight_body_mass` AND `lean_body_mass` (both are kg-canonical mass metrics; the task text named only weight, spec §4 says "for metrics in CANONICAL_UNITS … lb→kg").
  - °F→°C (°F−32)×5/9 pinned to `apple_sleeping_wrist_temperature` only (degF on any other canonical metric → unconvertible skip).
  - kJ→kcal ÷ 4.184 unchanged.
  - No-factor units (e.g. `gal`, `mi`) → row=None + `skip_reason="unit_unconvertible"` — the job-4 consumable out-signal (kept pure: no I/O).
- `metric_rows_detailed` now counts `unit_unconvertible` in its skip census (re-derives the reason through the gate itself for qty/date points that yielded no row with a parseable ts/qty). `UNIT_UNCONVERTIBLE` exported constant; module docstring iron-law wording updated.

## Scope fix (numbers in the task text)

Task expected 160 lb → 72.57 kg and 11.8 st → 74.93 kg. Those do not follow from the mandated factors:
- 160 ÷ 2.20462 = **72.5748655…** (72.57 is 160 × 0.45359237 — the exact kg-per-lb definition, a different formula than the mandated ÷2.20462)
- 12 ÷ 0.157473 = **74.9334806602…** (74.93 ÷ 12 implies a 6.35069 kg/st factor, not the mandated 0.157473 → 6.35029…)

Implemented with the MANDATED constants (spec §4 / decomposition both pin ÷2.20462 / ÷0.157473) and locked tests to the factor-true values 72.57 / 74.9334807. Scenario 7's 72.57 (abs=1e-4) tolerance is unaffected.

## Verification

- Unit: 490 passed (new TestCanonicalUnitConversion: lb/st/degF conversions, °F metric pinning, gal/mi visibly counted, bad-ts NOT counted as unit skip, pass-through intact, kg pass-through, constants exact).
- Acceptance (live pg): TestUnitConversion::test_scenario7_lb_converts_to_kg + test_scenario7_kj_converts_unchanged PASS.
- ruff clean.

## Known remaining acceptance failures (NOT job-3 scope, owned by other jobs)

- `test_unconvertible_unit_counted_visibly` — needs job-4's `skipped` object on the /api/ingest 200 body (normalize already supplies the count).
- ECG/HRN/MCP/schema/`test_no_silent_drops_in_response` failures — jobs 4–8.

## Interface contract for job-4

`skipped` may be composed from `metric_rows_detailed(document)[1]` — it already carries `UNIT_UNCONVERTIBLE` + `SLEEP_NO_VALUE`; ecg/notification/unrecognized_shape come from their own jobs. Counters never block ingest.
