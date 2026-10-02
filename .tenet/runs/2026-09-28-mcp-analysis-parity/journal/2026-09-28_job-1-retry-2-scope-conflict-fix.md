# job-1 retry 2: scope conflict fix

type: journal
source_job: 86202ea1-afa2-48e9-a18c-49aeb8fba2f0
job_name: Migration 0003 sleep_sessions
created: 2026-09-28T09:09:43.344Z

## Findings

- **category**: scope_conflict
- **commit_message**: tenet(job-1): fix python_files glob to match migration acceptance tests
- **commit_new**: 91e6a17
- **commit_old_removed**: a575932
- **job**: job-1 (Migration 0003 sleep_sessions)
- **old_job_commit**: {"2_files":["pyproject.toml","scripts/split_backfill.py (64 lines, unrelated backfill-chunking utility)"]}
- **reason**: code_critic retry-1 finding: job commit a575932 bundled the untracked 64-line scripts/split_backfill.py despite scope 'pyproject.toml only'
- **remediation**: history rewrite on local-only branch (no remote): detached at a575932, git rm --cached scripts/split_backfill.py, amend -> 91e6a17 (pyproject.toml only); replayed journal commit 983f14c -> 4759bc7, then amended with corrected SHA refs -> 570b929; split_backfill.py restored on disk UNTRACKED (?? scripts/), lint gate scoped to 'ruff check src/ tests/' (house gate) instead of committing the script
- **verification**: {"collect_acceptance":{"mcp_analysis_parity.py":26,"mcp_analysis_parity_migration.py":17,"parse_everything_migration.py":21},"git_show_stat_91e6a17":"pyproject.toml | 2 +- (only file)","ruff_check_src_tests":"All checks passed!","split_backfill_tracking":"untracked"}
