# job-6 mcp cli schema wiring partial eval split needed

type: journal
source_job: 2170538e-6bc8-44e4-b05c-5199663fe38d
job_name: MCP tool + CLI health sleep + schema entry
created: 2026-09-29T14:01:59.385Z

## Findings

- **context**: worker delivered all 3 wiring surfaces + tests but again left the tree uncommitted (3rd occurrence; orchestrator fallback commit 2d8a421). interaction_e2e exited rubric-less twice (attempt 2 spent 24min building a live-surface probe harness without converging) → split into reduced-scope probes.
- **coverage**: code/test/security/data-integrity/sleep-session-fidelity all PASS (fidelity ran whole, no slices needed; TestMcpToolSurface 2/2 under real postgres)
- **fallback_commit**: 2d8a421 tenet(job-6): wire MCP tool list_sleep_sessions + CLI health sleep + schema entry (6 files, +137/-9)
- **job**: job-6 MCP tool + CLI health sleep + schema entry
- **job_id**: job-6
- **lesson**: interaction_e2e needs a rubric-mandate + probe-scope cap in dispatch (live-surface probe harness building is where it stalls); scripted checks already green so split slice focuses on bounded exploratory probes only
- **status**: 5/6 PASS; interaction_e2e CRITIC_SPLIT_NEEDED — splitting into one bounded probe slice
- **worker_retries**: 0
