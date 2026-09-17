# Research: MCP Python SDK v2 — streamable HTTP server in a FastAPI app [scanned-not-verified]

Sources: py.sdk.modelcontextprotocol.io (fetched 2026-09-08), github.com/modelcontextprotocol/python-sdk README.

## Key findings

- **MCP Python SDK v2 is the current stable line** (24.2k stars, MIT, active). `pip/uv add "mcp[cli]"`. Python 3.10+. Supports 2026-07-28 spec and earlier revisions.
- Server: `from mcp.server import MCPServer`; `@mcp.tool()` (type hints = schema), `@mcp.resource("scheme://{param}")` templated resources.
- Transports: stdio (default), **streamable-http** (the deploy transport; SSE is legacy — do not use).
- `mcp.run(transport="streamable-http", port=NNNN)` builds a Starlette app + uvicorn; endpoint default `/mcp`.
- **Mounting inside an existing ASGI app is a supported path** ("Add to an existing app") — relevant for serving ingest (FastAPI) and MCP from one process; alternatively run two ASGI apps on separate ports. Mounting details to be confirmed in-slice; fallback = separate uvicorn workers on separate ports, which keeps concerns isolated and is acceptable on the server.
- HTTP transport options: `stateless_http=True` (fresh transport per request — good for simple stateless tool servers), `json_response=True` (no SSE stream; simpler for proxies but loses mid-request notifications), `max_request_body_size` (default 4 MiB), `transport_security` for DNS-rebinding protection when deployed.
- Client for tests: `Client("http://localhost:PORT/mcp")` — the SDK itself provides the e2e test surface (call tools/resources programmatically). No browser needed.
- v1 (`mcp<2`, FastMCP-style `mcp.server.fastmcp.FastMCP`) is legacy; **pin `mcp>=2`**.
- Auth: SDK leaves HTTP auth to the host layer — our FastAPI/Starlette middleware checks `Authorization: Bearer <token>` before the MCP endpoint; same secret as ingest token (single shared secret decision).

## Design implications for health-db

1. Pin `mcp>=2,<3` (v2 API surface).
2. Tools return structured content (`structured_content`) — SDK v2 supports structured output; keep tool payloads JSON-shaped and paginated.
3. Resources: `health://metrics`, `health://freshness`, `health://schema` — plain reads, no params needed (or templated e.g. `health://metric/{name}`).
4. For slice-2 tests: use SDK `Client` against a locally running streamable-http server (integration tests); unit-test tool functions directly.
5. MCP Inspector (`mcp dev`) for manual poking during development.

Confidence: [scanned-not-verified] — mounting pattern and exact v2 API names to be verified during slice-2 implementation; the fallback (separate ports) is low-risk.