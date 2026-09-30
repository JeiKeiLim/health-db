# job-5 list sleep sessions done via split critics

type: journal
source_job: 535c52da-a7a7-48de-93f8-95e756de7329
job_name: queries.list_sleep_sessions read layer
created: 2026-09-29T12:03:57.029Z

## Findings

- **attempt_history**: retry0: worker completed 92-line list_sleep_sessions + 12 unit tests but UNCOMMITTED (orchestrator fallback commit d5afed3); fidelity critic failed 2 consecutive no-rubric verdicts (narrative-only, then 2h timeout) → split; slices A (row mapping/paging/clamps/ts_kst) + B (acceptance scenarios 1/2/4/9 live pg) both PASS
- **commit**: d5afed3 (orchestrator fallback commit)
- **job**: job-5 queries.list_sleep_sessions read layer
- **job_id**: job-5
- **lesson**: workers keep leaving trees uncommitted; also sleep_session_fidelity critic slices must mandate early rubric emission AND budget output strictly (2h timeout on full-scope run)
- **out_of_scope_finding_noted**: queries.schema_resource does not advertise sleep_sessions table (TestMcpToolSurface.test_schema_resource_lists_sleep_sessions fails at mcp_analysis_parity.py:564) — that is EXPLICITLY job-6's deliverable (health://schema resource entry)
- **status**: EVAL_PASS via split (5 original critics + 2 fidelity slices all pass)
- **worker_retries**: 0
