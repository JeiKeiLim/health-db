# mcp server complete streamable-http 8 tools 3 resources bearer

type: journal
source_job: 00000000-0000-0000-0000-000000000000
job_name: unknown
created: 2026-09-10T07:12:10.975Z

## Findings

- **acceptance**: TestMcpSurface 10/10 + full tests/acceptance/slice_2_db_mcp.py 32/32 green (real streamable-http round-trips, ~15min runtime due to per-test containers)
- **auth**: ASGI BearerAuthMiddleware outermost on the Starlette app; constant-time sha256-digest compare (bearer_ok) against config.INGEST_SECRET read live; 401 before MCP dispatch (no tool enumeration pre-auth); verified fail-closed when secret None
- **decision_serve**: separate uvicorn port (8100) instead of ASGI-mount: MCPServer.streamable_http_app is a self-contained Starlette app with its own session-manager lifespan; nesting two lifespans adds ordering complexity for zero benefit. Documented in module docstring; decomposition allows fallback separate-port
- **entry_point**: health-mcp = health_db.mcp_server:main (uv run health-mcp)
- **env**: MCP_HOST/MCP_PORT (defaults 0.0.0.0:8100) documented in .env.example
- **fixtures**: mcp fixture added to tests/acceptance/conftest.py: seeds corpus via api fixture (metrics payload + workout w/ HR+route through the REAL ingest route), serves the MCP app in a background uvicorn thread on a random port, per-call SDK Client (stateless mode → no session state across loops)
- **implementation**: src/health_db/queries.py (shared read layer: list_metrics/query_day/get_metric_series/get_trends/list_workouts/get_workout_detail/freshness_check/raw_samples + schema_resource; UnknownMetric/UnknownWorkout/InvalidParameter actionable errors; raw pagination limit clamp [1,10000], next_offset None on last page; freshness = median consensus latest_complete_day + stale iff gap>1d); src/health_db/mcp_server.py (8 tools via MCPServer structured_output=True, from-alias via pydantic validation_alias for reserved-word args, ToolError guard wrapping so 'metric X not found — call list_metrics' reaches the client verbatim, BearerAuthMiddleware constant-time sha256 compare, stateless_http + json_response streamable-http at /mcp, health-mcp entry point on MCP_HOST/MCP_PORT default 0.0.0.0:8100)
- **sdk**: mcp 2.2.0 (uv.lock pins mcp>=2,<3 as required); key APIs verified in-slice: MCPServer(name,...).streamable_http_app(streamable_http_path=/mcp, json_response=True, stateless_http=True), server.tool()/server.resource() decorators, add_tool/remove_tool for guarded re-registration (add_tool alone KEEPS the original tool), ToolError→is_error verbatim text vs UnexpectedToolError masking, functools.wraps REQUIRED on tool wrappers or the SDK builds a wrapperArguments schema (*args), Client(server_or_transport) — Transport CM (streamable_http_client(url, http_client=...)) because Client has no http_client kwarg
- **smoke**: health-mcp started: no-auth POST /mcp → 401, wrong token → 401, valid Bearer → 200; SDK Client list_tools + health://schema read over real HTTP OK
- **structured_output**: dict[str, object] return annotations + structured_output=True yield structured_content on the wire; bare dict annotation fails SDK schema derivation (use dict[str, object])
