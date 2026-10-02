# critic tailoring: re-enabled data-integrity critic for this run

type: journal
source_job: 00000000-0000-4000-8000-000000000001
job_name: unknown
created: 2026-09-28T07:23:09.465Z

## Findings

- **detail**: Step 1 global-critic review for run 2026-09-28-mcp-analysis-parity: (1) security critic kept enabled — run touches ingest response accounting, a new migration, and a new MCP tool surface. (2) data-integrity critic RE-ENABLED for this run (was disabled; no recorded reason) — its focus (idempotency latest-wins, fixed-UTC+9 day boundaries, archive-first, transactionality, no double-counts) is exactly the risk surface of the new sleep_sessions lane. Will restore per run-end lifecycle unless the run surfaces a reason to keep it off.
