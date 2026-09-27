# job-8 eval retry 3 — consistent offset bigint boundary

Commit 77c89d46ef1aacbd8a2f7df13504157965769c73 — `tenet(job-8): consistent offset bigint boundary`.

## Fixes (code_critic findings from retry 2)

1. **Off-by-one**: `_validated_offset` guard changed from `offset >= OFFSET_MAX_BIGINT` to
   `offset > OFFSET_MAX_BIGINT` (queries.py:869). Constant stays `OFFSET_MAX_BIGINT = 2**63 - 1`
   (the max accepted value — PG serves an empty page for it without crashing). Docstring
   (queries.py:863-866), comment (queries.py:56-61), and error message
   ("must be at most 9223372036854775807") now state the consistent rule.
2. **Boundary tests**: added `test_offset_max_bigint_accepted` (2**63 - 1 accepted — the exact
   boundary) and kept `2**63` rejected via `test_offset_at_bigint_ceiling_is_actionable_not_db_crash`
   (match updated to "must be at most"). Dropped the duplicate
   `test_offset_ceiling_minus_one_accepted` (same value as `test_offset_just_below_ceiling_accepted`).

## Verification

- `uv run pytest tests/unit` → 630 passed
- `uv run pytest tests/acceptance/parse_everything.py -k TestMcpTools` → 4 passed
- `uv run ruff check src/ tests/` → All checks passed

Note: tenet_update_knowledge rejected job_id "job-8" (needs UUID); journal note written to file directly.
