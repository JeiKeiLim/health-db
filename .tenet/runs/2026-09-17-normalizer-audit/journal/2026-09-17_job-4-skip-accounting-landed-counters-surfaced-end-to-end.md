# job-4 skip accounting landed, counters surfaced end to end

type: journal
source_job: 0963bdbe-ecb6-4bcf-a3d0-4a25ca24430b
job_name: Skip accounting: PayloadReport.skipped + ingest response
created: 2026-09-17T14:33:16.763Z

## Findings

- **acceptance_status**: 8 parse_everything failures remain — all ECG/HRN-lane tests (TestEcgLane, TestNotificationLane, TestMcpTools lane cases) hitting 'empty v2 envelope' 400 because jobs 5/6/7/8 (lane normalizers, pipeline upserts, MCP tools) are not landed yet; all sleep/skip/anti-scenario acceptance tests pass, including test_no_silent_drops_in_response (sleep_no_value=1 asserted in the /api/ingest response) and test_unconvertible_unit_counted_visibly ('skipped' key present)
- **changed_test_contracts**: slice_1_ingest.STORED_RESPONSE_KEYS now 9 keys (+skipped, +SKIPPED_ACCOUNTING_KEYS set assertion); slice_2_db_mcp.test_non_canonical_unit_skipped updated for iron-law-4 amendment (lb converts to kg — old skip expectation was stale)
- **deliverables**: SKIPPED_KEYS frozen tuple + empty_skipped()/merge_skipped() in payloads.py; PayloadReport.skipped field with unrecognized_shape mirrored from unknown_formats at parse time; pipeline.IngestResult.skipped collecting metric_rows_detailed census (unit_unconvertible, sleep_no_value) via merge_skipped, duplicate short-circuit returns all-zero object; /api/ingest 200 response carries 'skipped' in BOTH paths (archive-only: parse counters via dict-union; pool: parse+store merged); replay.py ReplayReport.skip_accounting folds per-payload IngestResult.skipped across a run (_fold_result_skips tolerates plain-string seam results), stderr diagnostics line 'skipped-points — unit_unconvertible=… unrecognized_shape=… ecg=… notification=… sleep_no_value=…' plus skip_accounting in the JSON summary; ecg/notification counters default 0 and will be incremented by jobs 5/6 through the same merge path
- **pitfalls**: replay.main() calls config.load() which re-applies from os.environ — tests must monkeypatch.setenv('ARCHIVE_ROOT', ...) not just the module attribute, else the CLI scans data/archive
- **tests**: 11 new payloads tests (frozen-key contract, unrecognized mirroring, merge semantics, drift-guard on SKIPPED_KEYS tuple); 6 new replay tests (fold across payloads/pending, failed-payload exclusion, summary JSON, stderr diagnostics); unit 507 passed; ruff clean; slice_1/slice_2/slice_2_perf acceptance all green
