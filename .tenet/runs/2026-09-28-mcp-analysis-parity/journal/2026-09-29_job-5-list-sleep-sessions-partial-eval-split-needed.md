# job-5 list sleep sessions partial eval split needed

type: journal
source_job: 535c52da-a7a7-48de-93f8-95e756de7329
job_name: queries.list_sleep_sessions read layer
created: 2026-09-29T12:01:41.312Z

## Findings

- **context**: worker completed implementation but left changes uncommitted (2nd occurrence this run); orchestrator committed as d5afed3 per fallback-commit rule
- **coverage**: code/test/interaction-e2e/security/data-integrity all PASS with valid rubrics; 692 unit pass; 9/9 sleep acceptance scenarios pass; 12 remaining acceptance fails attributed to jobs 6-10
- **evidence_before_timeout**: mirrors list_ecgs; _ts_kst fixed +09:00 justified by spec exact assertion; 12/12 unit incl. real PG round-trips; migration 0003 additive fixed-UTC+9
- **fallback_commit**: d5afed3 tenet(job-5): add list_sleep_sessions read layer with paging (queries.py +92, test_queries.py +188)
- **job**: job-5 queries.list_sleep_sessions read layer
- **job_id**: job-5
- **lesson**: sleep_session_fidelity critic has a context-budget problem on full-scope verdicts (timed out at 7200000ms once before); future evals of this critic should mandate early rubric emission
- **status**: 5/6 critics PASS; sleep_session_fidelity_critic CRITIC_SPLIT_NEEDED (2 consecutive no-rubric exits: narrative-only, then 2h invocation timeout) — splitting into two reduced-scope slices
- **worker_retries**: 0
