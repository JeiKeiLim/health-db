# e2e-2 final gate green 781 passed corrected

type: journal
source_job: e0267826-c85f-4db6-8ca1-f2aa2749b3ae
job_name: Final E2E: full gate + deployment handoff doc
created: 2026-09-27T18:59:48.667Z

## Findings

- **acceptance_breakdown**: {"deselected_in_requires_pg_run":23,"note":"Prior e2e-1 run's 634/147 split reflected the same 781 total; 23 tests now collect as unit (deselected in requires_pg runs). Totals identical.","parse_everything_acceptance":"25/25","slice_1_ingest_plus_slice_2_plus_parse_everything_migration_plus_parse_everything":"122 passed (non-perf acceptance)","slice_2_perf":"2/2 (gate < 1s, non-regression confirmed)","unit_plus_acceptance_total":781,"unit_tests":"657 passed (34.6s)"}
- **corrected_grand_total**: {"double_counted_prior_total":783,"failed":0,"final":"781 passed = 657 unit + 124 acceptance (acceptance includes slice_2_perf 2/2; no double-count)","prior_reported_total":781}
- **e2e_role**: e2e-2 (final verification)
- **gate_commands**: {"lint":"uv run ruff check src/ tests/ -> All checks passed!","perf":"uv run pytest tests/acceptance/slice_2_perf.py -m requires_pg -> 2 passed","requires_pg":"uv run pytest tests/ -m requires_pg -> 124 passed (123.6s)","unit":"uv run pytest tests/ -m 'not requires_pg' -> 657 passed"}
- **status**: PASS
