# job-8 eval retry fixes complete

type: journal
source_job: 8a72f30e-b942-414f-af79-caeeb79aa109
job_name: MCP tools + queries + CLI + schema resource
created: 2026-09-19T16:26:51.345Z

## Findings

- **commits**: ["c5f6a39","fed75ca","9591738"]
- **files_changed**: ["src/health_db/queries.py","src/health_db/cli.py","tests/unit/test_queries.py"]
- **finding_2_error_surface**: Deleted dead UnknownHrn (queries.py) + its cli.py:359 catch — spec §7 defines no hrn-detail tool. Verified mcp_server guarded except-tuple already carries the remaining family (UnknownMetric/UnknownWorkout/UnknownEcg/InvalidParameter/ValueError → ToolError) — no change needed there; added UnknownEcg import + wire + CLI tests proving both surface verbatim.
- **finding_2_limit_default**: _bounded_limit(limit: int | None, default, maximum, label) — None → default; bool rejected; non-int raises InvalidParameter. All 3 call sites (list_ecgs, get_ecg_detail, list_heart_notifications) now take limit=None defaults and pass the advertised spec §7 default (50/1000/50).
- **gates**: 624 unit + 146 acceptance (incl. parse_everything TestMcpTools 4/4 + slice_2_db_mcp 43/43 — original 8 tool schemas untouched) + ruff clean.
- **lesson**: The docker-gated unit tests reuse the acceptance conftest helpers by loading tests/acceptance/conftest.py via importlib by file path (tests/ has no __init__.py so 'tests.acceptance' is not importable under pytest rootdir insertion).
- **retry_context**: Eval retry after code_critic + test_critic findings on 084d898/6fb7589. Commit 1 = the interaction_e2e 4-line alias fix verified present in working tree (alias 'h' → 'n' matching _day_range_where('n',...)) — committed first as directed. Tests added: 33 new (TestBoundedLimit 7, TestValidatedOffset 5, TestUnknownEcg 1, TestConstants lane bounds 1, TestLaneQueriesAgainstPostgres 8 — pagination walk 2→4→…→None with limit+1 lookahead trim + 10001→10000 clamp, from/to day filters for both lanes, unknown/non-int id raises; TestLaneMcpRoundTrip 6 — real uvicorn streamable-http wire through guarded tools incl. ToolError verbatim delivery; TestLaneCliSurfacing 3).
