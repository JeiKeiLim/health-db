# slice-1-quarantine complete all critics pass

type: journal
source_job: 2fa9f9a3-577b-4dd7-a41e-780a1b8876d1
job_name: Malformed/oversize failure semantics
created: 2026-09-08T11:23:45.955Z

## Findings

- **dag_id**: slice-1-quarantine
- **eval**: ALL 5 critics PASS first-try
- **failures_surface**: handle_malformed(body, exc) → 400 {error:'malformed', quarantine:<id>} with quarantine write BEFORE response; check_size(length) → raises OversizedPayload → 413 with nothing archived (Retry-After belongs to 503 not 413, corrected during implementation); quarantine lane excluded from dedup index
- **worker_commit**: 23e3592; 23 tests, unit suite 202 passed, ruff clean
