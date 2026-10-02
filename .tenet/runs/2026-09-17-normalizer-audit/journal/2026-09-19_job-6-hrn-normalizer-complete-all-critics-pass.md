# job-6 hrn normalizer complete all critics pass

type: journal
source_job: 839f3968-24af-4295-996f-a468e9c87cd3
job_name: HR-notification lane normalizer (pure)
created: 2026-09-19T09:03:59.254Z

## Findings

- **commits**: ["5084dda","eea09bf"]
- **job**: job-6
- **lesson**: HR-notification lane normalizer landed: HrnRow/HrnSampleRow + _hrn_rows(document)->(events, samples, skips); kinds hr/hrv; threshold guarded to signed INT32 range [HRN_INT_FIELD_MIN=-2^31, 2^31-1] (INT32_MIN kept); numeric-magnitude overflow (10**400, int(1e308), NaN) -> drop+count under 'notification'. CRITICAL LESSON FROM RETRY 1: when adding try/except around _num, do NOT delete the adjacent `if value is None: dropped += 1; continue` branch — guards must complement, not replace, existing drop checks (all 5 critics caught the regression; job-5's _ecg_voltage_rows normalize.py:1013-1015 is the canonical both-checks shape). OUTSTANDING FOR PRE-E2E-1 DISPATCH: ECG voltage VALUE overflow guard (huge-int voltage literal crashes _ecg_voltage_rows, job-5 surface) — code_critic residual-defect finding from job-6 retry 1; I will fold it into job-7's prompt as it owns pipeline wiring and the same file pattern. 557 unit tests green, ruff clean.
- **retries**: 2 (product_bug overflow guard; regression fix in retry 2)
- **run**: 2026-09-17-normalizer-audit
- **status**: complete
