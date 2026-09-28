# critic tailoring: swept orphaned run-scoped critics

type: journal
source_job: 00000000-0000-4000-8000-000000000000
job_name: unknown
created: 2026-09-28T07:23:03.778Z

## Findings

- **detail**: Step 0 orphan sweep: dropped run-scoped critic 'parse-everything' (prompt_file pointed at .tenet/runs/2026-09-17-normalizer-audit/critics/parse-everything.md — a prior run whose lifecycle never ran). Its focus (zero-silent-drops, sentinel handling, census fidelity, new-lane idempotency, migration safety) is highly relevant to THIS run and will be re-generated as a run-scoped critic for 2026-09-28-mcp-analysis-parity, extended with the sleep-session lane's specific risks. Roster now: code_critic, test_critic, interaction_e2e, security (enabled), data-integrity (disabled).
