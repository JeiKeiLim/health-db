# job-8 mcp tools complete all critics pass

type: journal
source_job: 8a72f30e-b942-414f-af79-caeeb79aa109
job_name: MCP tools + queries + CLI + schema resource
created: 2026-09-27T13:18:16.100Z

## Findings

- **commits**: ["084d898","6fb7589","c5f6a39","fed75ca","9591738","cbac809","3939e21","77c89d4","26f697b"]
- **job**: job-8
- **lesson**: MCP surface complete: list_ecgs / get_ecg_detail (paged voltages, offset bounded to bigint range with consistent > rule) / list_heart_notifications (from/to KST filters, alias n fixed) registered in mcp_server (11 tools), CLI health ecgs/ecg-detail/hrn, schema_resource complete. Dead code removed (UnknownHrn, label param); _bounded_limit implements default. 33+6 new tests: pagination walk (offset 2->4->None, lookahead trim), UnknownEcg paths incl. real MCP wire round-trips, validation clamps, day filters, boundary 2^63. 630 unit green, TestMcpTools 4/4, slice_2_db_mcp 43/43 (existing 8 tools untouched). NOTE FOR e2e-2: deployed MCP at server.example:8443 is stale (lacks new tools) - redeploy required for parity; interaction_e2e verified against local code not deployment. ALL 24 parse_everything acceptance tests should pass now.
- **retries**: 3 (alias fix + dead param + tests; lane-label/offset-bound; boundary off-by-one)
- **run**: 2026-09-17-normalizer-audit
- **status**: complete
