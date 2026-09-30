# 2026-09-29 — job-3 test-bug fix (retry 1): DO UPDATE SET completeness asserted

## Context
Test critic rejected job-3's first attempt: test_lane_persistence.py asserted
UPSERT_SLEEP_SESSION's shape (conflict target, RETURNING id, ingested_at=now(),
13 placeholders, column order) but not DO UPDATE SET completeness — a mutation
dropping any `col = EXCLUDED.col` entry among the 11 payload columns would stay
green and silently preserve stale stage values on re-ingest of the same
(source_name, sleep_start).

## Done
- helpers.py NOT touched (commit 491092d verified correct: all 11 columns
  record_date, sleep_end, in_bed_start, in_bed_end, total_sleep, asleep, deep,
  core, rem, awake, in_bed each appear as `col = EXCLUDED.col`).
- Added `test_sleep_session_upsert_updates_every_payload_column` to
  tests/unit/test_lane_persistence.py — text-shape assert (no docker needed)
  looping all 11 columns with failure messages naming the regression risk.
- `uv run pytest tests/unit/test_lane_persistence.py -q` → 26 passed.
- `uv run ruff check src/ tests/` → clean.
- Committed 5f6b1a4 `tenet(job-3): assert DO UPDATE SET covers all 11 payload
  columns` (only the test file staged; untracked scripts/ left unstaged per
  scope).