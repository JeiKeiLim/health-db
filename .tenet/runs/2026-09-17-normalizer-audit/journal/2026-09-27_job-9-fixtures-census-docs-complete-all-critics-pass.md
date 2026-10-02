# job-9 fixtures census docs complete all critics pass

type: journal
source_job: c07bf106-a009-4959-b8cd-9fde5e692490
job_name: Fixtures, census lock, docs + doctrine note
created: 2026-09-27T16:54:52.350Z

## Findings

- **commits**: ["799aabd","04596cf","4ddcd77","60f0da6"]
- **job**: job-9
- **lesson**: Census fixtures + lock landed: tests/fixtures/parse_everything/ (sleep_shape_matrix.json with census weights 130 recent/2500 backfill + expected_qty, ecg_session.json, hr_notification.json); test_census_lock.py pins (count, qty_sum) per row name - mutation-verified to reject both the count/split drift AND the canonical asleep:0 sentinel regression (qty 0.0). README: iron-law-4 exact-factor amendment + deploy-BEFORE-backfill ordering warning with replay/verify steps (6.21/6.34/6.37). Doctrine-drift journal note written for project/design.md. unit 634 green, acceptance 25 green, ruff clean. Minor cosmetic: journal cites dangling pre-amend commit 33dc371 - no functional impact.
- **retries**: 2 (empty-output retry; census-lock qty pin)
- **run**: 2026-09-17-normalizer-audit
- **status**: complete
