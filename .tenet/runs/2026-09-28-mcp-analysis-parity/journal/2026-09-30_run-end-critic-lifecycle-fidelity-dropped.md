# run-end critic lifecycle fidelity dropped

type: journal
source_job: fdc9839c-8a1c-4193-9c38-02a1d50519ff
job_name: Integration: full parity battery
created: 2026-09-30T04:17:01.807Z

## Findings

- **action**: dropped run-scoped critic sleep-session-fidelity from roster at run end (default: drop — it caught real failures that triggered fixes, but the risk is feature-specific, not repo-wide; the prompt file stays with the run). data-integrity global critic was re-enabled during critic tailoring and stays enabled. Count: 1 dropped, 0 promoted, 0 disabled-restored.
- **rationale**: sleep-session-fidelity risk (verbatim field preservation for the sleep_sessions lane) is feature-specific: once the lane is cut over, no future job touches those invariants unless the lane changes. The critic passed on every dev job it ran against after job-2's overflow fix; its unique value was bound to this run's scope.
- **trigger_evidence**: data-integrity (global) caught the job-2 overflow guard bug and job-10's runbook bugs; test_bug on job-3/9 came from test_critic (global); the run-scoped fidelity critic mostly corroborated global critics — no unique repo-wide risk class observed
