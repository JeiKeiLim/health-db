# job-2 skipped keys normalizer session rows done

type: journal
source_job: 642b50a9-8145-4ba7-9c91-3610ed0b1724
job_name: SKIPPED_KEYS + normalizer session rows
created: 2026-09-29T01:47:46.429Z

## Findings

- **attempt_history**: retry0: data-integrity slice1 FAIL product_bug — _num overflow guard missing (10**400 JSON-int crash vector); retry1: guard added mirror _hrn_sample_rows (e906106) but journal claimed 31 tests vs actual 30 (evidence_mismatch); retry2: journal count corrected (132921f). All 6 critics pass.
- **commit**: e906106 (code) + 132921f (journal count)
- **danger_zones_held**: census lock green untouched; sleep_shape_matrix.json untouched; existing aggregated-sleep branch emissions unchanged; _rows_for_point untouched
- **docker_note**: Docker VM disk was 100% full (2344 stale volumes, 114.8GB) causing pg-container failures; fixed via docker volume prune -f mid-run
- **job**: job-2 SKIPPED_KEYS + normalizer _sleep_session_rows
- **job_id**: job-2
- **lesson**: dev jobs writing journal counts must run pytest --collect-only fresh; include 'scope ruff to src/ tests/' and 'do not stage unrelated untracked files' in every dev prompt (both patterns bit on job-1)
- **worker_retries**: 2
