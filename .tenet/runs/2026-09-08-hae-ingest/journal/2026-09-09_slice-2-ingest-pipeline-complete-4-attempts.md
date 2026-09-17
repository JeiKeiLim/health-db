# slice-2-ingest-pipeline complete 4 attempts

type: journal
source_job: fb46aec4-85a0-4d3d-a08d-abac62cade26
job_name: Ingest pipeline: normalize→upsert→rollup, 503+pending on DB outage
created: 2026-09-09T23:37:20.087Z

## Findings

- **[confidence]**: implemented-and-tested
- **commits**: ["a3b7e38","3045582","6712fef","3c22a5f"]
- **job**: slice-2-ingest-pipeline (fb46aec4) complete after 4 attempts
- **note_1**: Attempt 1 context-limited post-exploration; attempt 2 hit 2h invocation timeout mid-increment-3 (commits a3b7e38+3045582); attempt 3 made fixture seam fixes then truncated (committed 6712fef); attempt 4 diagnosed root causes, proved 16/16 pg acceptance green but hit 2h timeout before committing (committed 3c22a5f by orchestrator fallback).
- **note_2**: Root causes found in attempt 4: (1) conftest api fixture monkeypatched get_pool without materializing module-global _pool singleton -> existing_pool() None -> route env-branch fallback; (2) main.py from-import froze the seam binding at first import time -> order-dependent test failures. Fix: materialize singleton with same conn kwargs as pg fixture (incl. options search_path).
- **note_3**: slice_2_db_mcp.py TestMcpSurface 10 tests intentionally ERROR with fixture 'mcp' not found until slice-2-mcp-server job adds the mcp fixture - documented out-of-scope pattern, matches slice-1 era placeholder semantics.
- **note_4**: Orphaned eval containers accumulate after 2h-timeout kills (eval-cr-pg found 10h old) - clean docker ps before pg gates.
- **verdict**: 16/16 pipeline+outage pg acceptance green; unit 329; ruff clean. Awaiting eval gate.
