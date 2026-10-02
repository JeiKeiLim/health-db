# job-8 eval retry 2: lane label + offset bound

type: journal
source_job: 8a72f30e-b942-414f-af79-caeeb79aa109
job_name: MCP tools + queries + CLI + schema resource
created: 2026-09-27T11:35:03.107Z

## Findings

- **acceptance**: parse_everything TestMcpTools 4/4 + slice_2_db_mcp 43/43
- **commit**: 3939e21
- **finding_1_dead_label**: _validated_lane_id dropped the dead 'label' param (sole caller passed 'ECG', body never used it). Removing is the cleaner critic-approved option: UnknownEcg.__init__ already formats the lane-named actionable message (id + list_ecgs) and is constructed directly at queries.py:727 (not-found path) and in 3 unit tests + 2 acceptance tests — threading label into the helper would double-wrap or fork message text. Call site updated to _validated_lane_id(ecg_id, UnknownEcg).
- **finding_2_offset_bound**: OFFSET_MAX_BIGINT = 2**63 - 1 constant added next to the lane limit constants; _validated_offset now raises InvalidParameter('voltage_offset must be below 9223372036854775807 (PostgreSQL OFFSET is a bigint)') for offsets >= 2**63 — before any pool borrow, so no NumericValueOutOfRange can reach the SQL layer. Boundary tests: 2**62 and 2**63-2 pass through unclamped, 2**63 raises, negative->0 clamp unchanged. Existing 8 tool schemas untouched (helper is job-8-private).
- **gates**: 630 unit + parse_everything TestMcpTools 4/4 + slice_2_db_mcp 43/43 + ruff clean
- **tests_added**: 5 in TestValidatedOffset (bigint ceiling raise, 2**62 boundary, 2**63-2, ceiling-1, plus existing 5) + TestValidatedLaneId 2 (pass-through + non-int raises UnknownEcg with list_ecgs message) = 7 new unit tests
