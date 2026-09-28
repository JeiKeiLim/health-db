# job-1 retry: harness fix — python_files glob

## What was wrong (code_critic, category: harness_bug)

`pyproject.toml:38` `python_files` contained the exact-name pattern
`mcp_analysis_parity.py` (no wildcard), so the new
`tests/acceptance/mcp_analysis_parity_migration.py` from job-1 was NOT
collected by a default `pytest` run — the run-level full-suite gate
silently omitted the 17 migration acceptance tests.

## Fix

- `python_files` pattern changed to `mcp_analysis_parity*.py` (scoped to
  pyproject.toml only; no migration SQL or test files touched).
- `scripts/split_backfill.py` (untracked utility left over from job-1)
  stays UNTRACKED and out of the job commit; the lint gate for this
  verification is scoped to `ruff check src/ tests/` (house gate).

## Verification

- `uv run pytest --collect-only tests/acceptance/ -q`:
  - `tests/acceptance/mcp_analysis_parity.py: 26` (unchanged)
  - `tests/acceptance/mcp_analysis_parity_migration.py: 17` (now collected)
  - `tests/acceptance/parse_everything_migration.py: 21`
- Full suite: 832 tests collected by default.
- `uv run pytest tests/acceptance/mcp_analysis_parity.py -q` still 26.
- `uv run ruff check .` → All checks passed!

## Commit

`91e6a17 tenet(job-1): fix python_files glob to match migration acceptance tests`
(contains pyproject.toml ONLY; retry 2 remediation removed the unrelated
`scripts/split_backfill.py` that retry 1 wrongly bundled in a575932 —
now untracked on disk again)