# slice-1-auth complete all critics pass

type: journal
source_job: be7e1820-b82e-4e0a-b2b9-c56115314057
job_name: Token auth + audit
created: 2026-09-08T09:46:05.054Z

## Findings

- **dag_id**: slice-1-auth
- **eval_round1**: code_critic FAIL (evidence_mismatch: worker mislabeled 41 pre-existing pytest setup errors as ruff lint errors) + interaction_e2e NO-RUBRIC → retried job with corrected-evidence enhanced prompt
- **eval_round2**: ALL 5 critics PASS (code/test/interaction_e2e/security/data_integrity). 34 auth tests, 123 unit total; 41 pre-existing acceptance errors correctly attributed: 15 slice-1 missing health_db.main + 26 slice-2 missing pg fixtures, zero lint-related
- **implementation**: constant-time compare over SHA-256 digests, fail-closed on unset/blank INGEST_SECRET, generic 401 (no token/body echo), frozen AuditEntry(ts/ip/ua/reason), router-level Depends, auth-before-body-read
- **lesson**: Workers must report evidence categories accurately — evidence_mismatch is retry-able via enhanced_prompt demanding correct attribution. Sub-agent tasks can return empty; orchestrator fallback: direct tenet_job_wait + tenet_start_eval + delegate eval-gather only.
