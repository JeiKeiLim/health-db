# critic tailoring: parse-everything critic enabled, data-integrity disabled for this run

type: journal
source_job: 00000000-0000-4000-8000-000000000001
job_name: unknown
created: 2026-09-17T05:03:25.894Z

## Findings

- **job_name**: critic-tailoring (manual phase step)
- **notes**: Step 0 orphan sweep: none — both custom critics are global-scope (.tenet/critics/), no run-scoped leftovers. Step 1: security stays enabled (unchanged surfaces, new MCP tools inherit Bearer). data-integrity DISABLED for this run: its prompt mandates 'non-canonical units must be skipped, only kJ→kcal allowed' — the user-approved iron-law-4 amendment (lb→kg ÷2.20462, st→kg ÷0.157473, °F→°C) deliberately violates exactly that clause; the critic would fail every correct job. Its still-valid focus (idempotency, day boundaries, transactionality, archive-first) is carried forward into the run-scoped parse-everything critic. Step 2: one run-scoped critic generated at .tenet/runs/2026-09-17-normalizer-audit/critics/parse-everything.md — silent drops, sentinel handling (50 mixed-stage nights), census lock, new-lane idempotency, migration safety, repair-path honesty. Restore data-integrity to enabled at run end (update its unit-policy clause to the amended iron law first).
- **run**: 2026-09-17-normalizer-audit
- **step**: Critic Tailoring (phases/02-spec-and-harness.md §4.5)
