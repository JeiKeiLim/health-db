# critic lifecycle: promoted data-integrity and security to global

type: journal
source_job: 34b9c6a3-f427-4bc8-8e6a-f4af1e13758e
job_name: fix-workout-normalizer
created: 2026-09-14T02:31:58.287Z

## Findings

- **confidence**: decision-only
- **note**: run-scoped data-integrity + security critics promoted to GLOBAL: both caught real failures this run (ULP rollup drift + missing-commit evidence_mismatch; real-PII fixture) and their risks (PHI data integrity, secrets/PII in a health-data repo) are repo-wide, not feature-specific. Prompt files moved to .tenet/critics/, roster prompt_file paths updated. These apply to every future eval round.
- **promoted**: ["data-integrity","security"]
- **run**: 2026-09-08-hae-ingest (post-run continuation: fix-workout-normalizer)
