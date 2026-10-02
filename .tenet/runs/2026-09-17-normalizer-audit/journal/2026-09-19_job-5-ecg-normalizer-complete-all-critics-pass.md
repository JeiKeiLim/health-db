# job-5 ecg normalizer complete all critics pass

type: journal
source_job: 5bd9678d-dae8-4b1c-9284-594abf39e219
job_name: ECG lane normalizer (pure)
created: 2026-09-19T07:26:46.161Z

## Findings

- **commits**: ["e60c081","c5a1d3f","af9049c"]
- **job**: job-5
- **lesson**: ECG lane normalizer landed: EcgRow/EcgVoltageRow frozen dataclasses + _ecg_rows(document)->(sessions, voltages, skips); UNIX-epoch voltage ts (NOT CF-absolute); per-point epoch range guard (OverflowError/OSError/ValueError -> drop+count under 'ecg' skip key) protects the full-history backfill from single corrupt points aborting the payload. voltage_count = declared or len(parsed) is job-7's contract to honor. FOR JOB-7: voltage VALUE inf/NaN with finite date passes _num and would flow to PG double precision - pipeline-layer guard candidate. FOR job-7/spec: resolve the lane-only-payload 400-vs-200 envelope conflict before wiring (spec section 2 vs acceptance test expectation). 530 unit tests green, ruff clean.
- **retries**: 1 (product_bug epoch-range guard)
- **run**: 2026-09-17-normalizer-audit
- **status**: complete
