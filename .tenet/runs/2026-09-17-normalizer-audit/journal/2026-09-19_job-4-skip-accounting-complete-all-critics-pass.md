# job-4 skip accounting complete all critics pass

type: journal
source_job: 0963bdbe-ecb6-4bcf-a3d0-4a25ca24430b
job_name: Skip accounting: PayloadReport.skipped + ingest response
created: 2026-09-19T04:36:22.962Z

## Findings

- **commits**: ["c42565e","2f23a7c","1c7b0a8"]
- **job**: job-4
- **lesson**: Skip accounting landed: PayloadReport.skipped (frozen 5 keys) + /api/ingest response in both paths + replay stderr diagnostics + _fold_parse_skips for CLI/API parity. ALL 5 critics PASS on re-eval after docker outage (0 retries). INCIDENT POSTMORTEM: host disk filled (9.5GB free/926GB), Docker.raw grew to 289GB sparse, docker daemon wedged ~12h, VM disk deleted during cleanup, reboot fixed it. OPERATIONAL LESSON: when workers run docker-based pytest concurrently, disk pressure can wedge docker and kill evals - watch df during heavy eval phases. Informational for job-9: (a) recognized-shapes-without-date dropped with no counter - consider missing_timestamp counter; (b) gal fixture + SKIPPED_KEYS drift guard deferred items.
- **retries**: 1 (product_bug replay unrecognized_shape) + eval re-dispatch after docker restoration
- **run**: 2026-09-17-normalizer-audit
- **status**: complete
