# e2e-1 full parity battery verified run complete

type: journal
source_job: fdc9839c-8a1c-4193-9c38-02a1d50519ff
job_name: Integration: full parity battery
created: 2026-09-30T04:15:13.698Z

## Findings

- **battery_results**: 914 non-pg + 167 requires_pg passed with 0 skipped; 26/26 mcp_analysis_parity accounted; census lock 4/4 green; ruff clean; 0001/0002 + sleep_shape_matrix.json diffs 0 lines; SKIPPED_KEYS superset with sleep_session; rollup-leak SQL proof 0 rows
- **blocking_findings**: NONE — no tenet_report_blocking_finding called; all critic findings arrays empty
- **critic_retries**: code_critic + interaction_e2e each had 1 narrative-only/no-rubric first exit, both passed on retry 1
- **harness_recommendation_followup**: pg acceptance fixture teardown uses bare 'docker rm -f' without -v, leaking one anonymous volume per fixture container (2427 orphans ~119GB accumulated); recommend teardown docker rm -fv + periodic volume prune — outside run scope, candidate for a future doctrine/knowledge note
- **job**: e2e-1 Integration: full parity battery (report-only)
- **job_id**: e2e-1
- **lesson**: docker volume leak from pg fixture teardown (rm -f without -v) is the run's recurring infra drag; fix in a future harness job
- **result**: FULLY VERIFIED — run complete
