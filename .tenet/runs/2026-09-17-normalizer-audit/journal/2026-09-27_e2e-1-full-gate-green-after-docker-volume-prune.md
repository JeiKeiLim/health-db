# e2e-1 full gate green after docker volume prune

type: journal
source_job: e0267826-c85f-4db6-8ca1-f2aa2749b3ae
job_name: Final E2E: full gate + deployment handoff doc
created: 2026-09-27T17:49:43.672Z

## Findings

- **docker_disk_exhaustion**: e2e-1 retry #1 first hit 'No space left on device' during postgres initdb: 2,346 anonymous hex-named volumes (115.4GB, 0 active containers) from long-gone disposable test containers filled Docker's disk. Fixed by removing ONLY anonymous volumes (docker volume ls -q | grep -v '^podcast-gen-web-service' | xargs docker volume rm) — the 4 named podcast-gen volumes preserved. Verified postgres:18 boots + pg_isready green afterwards. Prevention note for future pg-gate runs: acceptance conftest teardown force-rms containers but anonymous volumes can leak across crashed runs; periodically prune.
- **full_gate_results**: {"acceptance_breakdown":{"parse_everything":25,"parse_everything_migration":21,"slice_1_ingest":23,"slice_2_db_config":7,"slice_2_db_mcp":43,"slice_2_schema":26},"grand_total":"783 passed, 0 failed","parse_everything_details":"25/25 (sleep rule matrix, in-bed split, ECG+HRN lanes, idempotency, lb conversion + visible unconvertible skip, force-replay repair, MCP tools, census lock, no-silent-drops, migration additive-only)","perf_gate":"tests/acceptance/slice_2_perf.py 2/2 passed, 90-day series <1s non-regression intact","ruff":"clean (src/ tests/)","unit":"634 passed (incl. 59 lane-query/MCP-round-trip tests against disposable pg)","whole_acceptance_dir":"147 passed in 124s"}
