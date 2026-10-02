# job-4 critic fix: replay folds parse-level skip census

type: journal
source_job: 0963bdbe-ecb6-4bcf-a3d0-4a25ca24430b
job_name: Skip accounting: PayloadReport.skipped + ingest response
created: 2026-09-17T17:55:52.586Z

## Findings

- **acceptance_status**: job-4's two flagged acceptance tests (test_no_silent_drops_in_response, test_unconvertible_unit_counted_visibly) pass; full unit suite 610 passed; ruff clean
- **changed_test_contracts**: none — new tests only (2 added to TestSkipAccounting in tests/unit/test_replay.py: dir-path and pending-path unrecognized_shape folding)
- **deliverables**: replay.py: _fold_parse_skips(report, body) — calls payloads.parse(body).skipped and merge_skipped into report.skip_accounting — invoked in BOTH replay_dir and replay_pending after a successful apply (alongside _fold_result_skips), restoring parity with main.py's parse+store merge for the live /api/ingest response; MalformedPayload defensively treated as no-census (cannot occur — body already json.loads-gated); module docstring counting contract updated to document the two merge sources
- **pitfalls**: The fold must happen on the REPLAY side (not pipeline.ingest_payload) — replay passes the already-decoded document into ingest_payload, and adding a parse inside the store transaction would double-parse and widen the store-stage contract; folding after the successful apply keeps the 'failed stores contribute no census' invariant; parse-level unrecognized_shape now appears in replay diagnostics even when the injected ingest seam is a plain-string fake
- **tests**: uv run pytest tests/unit/test_replay.py tests/unit/test_payloads.py -q → all pass; uv run pytest tests/acceptance/parse_everything.py -k 'test_no_silent_drops_in_response or test_unconvertible_unit_counted_visibly' -v → 2 passed; full suite 610 passed; ruff clean
- **verdict**: code_critic product_bug resolved: ReplayReport.skip_accounting + stderr skipped-points now report the parse-level census (unrecognized_shape) identically to the live ingest response — zero-silent-drops contract holds on the force-repair path
