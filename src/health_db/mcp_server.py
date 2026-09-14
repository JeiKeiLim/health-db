"""MCP streamable-HTTP server for health-db (job slice-2-mcp-server).

Exposes the shared query layer (:mod:`health_db.queries`) as 8 MCP tools +
3 resources over **streamable-http** at ``/mcp`` (spec §MCP query surface),
guarded by a Bearer-token ASGI middleware (constant-time compare against
``config.INGEST_SECRET`` — the same shared secret the ingest endpoint uses,
spec §Auth Flow step 3).

Serving strategy (research/mcp-python-sdk.md "Add to an existing app",
verified in-slice against the installed ``mcp`` 2.2.0 SDK): the SDK's
``MCPServer.streamable_http_app(...)`` returns a self-contained Starlette
application with its own session-manager lifespan, so the chosen strategy is
a **separate uvicorn app/port** from the ingest service. The ingest app
(:mod:`health_db.main`) stays plain FastAPI on :8000; this module serves MCP
on :8100 by default (``MCP_HOST``/``MCP_PORT`` env). Mounting one ASGI app
inside the other was rejected because the MCP session manager's lifespan
runs via its own Starlette app and nesting two lifespans (FastAPI +
session-manager) adds ordering complexity for zero product benefit — the
decomposition explicitly allows "fallback separate-port OK".

Transport options (research): ``stateless_http=True`` (fresh transport per
request — no session tracking; simple, proxy-friendly, and correct for a
single-user query surface) and ``json_response=True`` (single JSON response
per request, no SSE stream). Both were verified against the official SDK
``Client`` over real HTTP round-trips (spike + acceptance fixture).

Auth (anti-scenario 6): the middleware runs BEFORE any tool dispatch on the
mounted Starlette app — an unauthenticated or wrong-token request is 401'd
without reaching the MCP protocol handler, so no tool enumeration or data
exposure is possible pre-auth. The comparison is constant-time over SHA-256
digests (harness iron law 3, :func:`health_db.auth.check_token` reuse) and
the secret itself never appears in any response or log (iron law 1).

Tools return **structured content** (``structured_output=True`` +
``dict[str, object]`` annotations, verified wire-compatible with the SDK
Client) so MCP clients receive both the JSON object and a text rendering.
Expected failures raise :class:`mcp.server.mcpserver.exceptions.ToolError`
so the message (e.g. "metric 'X' not found — call list_metrics") reaches
the agent verbatim in an ``is_error`` result — an unexpected exception
would be masked as "Error executing tool <name>" and lose the actionable
hint.

The pool is resolved lazily per request through the same seams the ingest
route uses (``db.pool`` module attributes at call time), so the pytest
``mcp`` fixture can pin the singleton to a disposable container.
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import inspect
import os
from typing import Annotated

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Scope, Send

import health_db.config as config
import health_db.queries as queries
from health_db import __version__

ALL_METRICS = Annotated[str, Field(description="metric name; empty = all metrics")]

# --- Env keys (documented in .env.example + README) ---
ENV_MCP_PORT = "MCP_PORT"
ENV_MCP_HOST = "MCP_HOST"

DEFAULT_MCP_HOST = "0.0.0.0"
DEFAULT_MCP_PORT = 8100
MCP_PATH = "/mcp"

MCP_SERVER_NAME = "health-db"


def _int_env(environ: dict[str, str], key: str, default: int) -> int:
    raw = environ.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def mcp_host(environ: dict[str, str] | None = None) -> str:
    """MCP bind host (``MCP_HOST``, default ``0.0.0.0``)."""
    import os as _os

    source = dict(_os.environ if environ is None else environ)
    value = source.get(ENV_MCP_HOST, "")
    return value.strip() or DEFAULT_MCP_HOST


def mcp_port(environ: dict[str, str] | None = None) -> int:
    """MCP bind port (``MCP_PORT``, default 8100; invalid → default)."""
    source = dict(os.environ if environ is None else environ)
    return _int_env(source, ENV_MCP_PORT, DEFAULT_MCP_PORT)


def bearer_ok(header_value: str | None, expected_secret: str | None) -> bool:
    """Constant-time check of ``Authorization: Bearer <secret>`` (fail-closed).

    ``None``/missing secret or missing/malformed header → False; matching
    scheme+token → True. Hashes both sides to fixed-length digests first so
    timing never depends on input lengths (harness iron law 3).
    """
    if not expected_secret or not header_value:
        return False
    prefix = "bearer "
    if not header_value.lower().startswith(prefix):
        return False
    token = header_value[7:]
    return hmac.compare_digest(
        hashlib.sha256(token.encode("utf-8")).digest(),
        hashlib.sha256(expected_secret.encode("utf-8")).digest(),
    )


class BearerAuthMiddleware:
    """ASGI middleware: require ``Authorization: Bearer <INGEST_SECRET>``.

    Installed outermost on the MCP Starlette app so a 401 never reaches the
    MCP transport (no session, no tool dispatch, no resource read). The 401
    body is a fixed JSON shape; no secret material is echoed (iron law 1).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Send, send: Send) -> None:  # type: ignore[override]
        if scope["type"] != "http":
            await self.app(scope, receive, send)  # type: ignore[arg-type]
            return
        headers = dict(scope.get("headers") or [])
        auth_value = (headers.get(b"authorization") or b"").decode("latin-1")
        if bearer_ok(auth_value, config.INGEST_SECRET):
            await self.app(scope, receive, send)  # type: ignore[arg-type]
            return
        response = JSONResponse(
            {"detail": "unauthorized: missing or invalid bearer token"},
            status_code=401,
        )
        await response(scope, receive, send)


def build_mcp_server(pool: object | None = None):
    """Build the ``MCPServer`` with 8 tools + 3 resources registered.

    ``pool`` is injected into every tool (the pytest ``mcp`` fixture pins a
    disposable container pool; production passes ``None`` and
    :func:`queries._resolve_pool` falls back to the materialized singleton).
    Tool functions are *sync* — the SDK runs them on worker threads, so a
    blocking psycopg borrow never stalls the event loop.
    """
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(
        MCP_SERVER_NAME,
        title="health-db",
        description=(
            "Personal health data (Health Auto Export → PostgreSQL): "
            "metric catalog, daily rollups, workouts, raw samples"
        ),
        version=__version__,
        warn_on_duplicate_tools=False,
    )

    def guarded(fn):
        """Rebind a registered tool's body so actionable errors reach the client.

        The SDK masks unexpected tool exceptions as "Error executing tool
        <name>" (no cause text), which would lose the "call list_metrics"
        hint; :class:`ToolError` messages are delivered verbatim. The
        wrapper keeps the wrapped function's signature (``__wrapped__``) so
        argument schemas come from the original tool, not ``*args``.
        """

        @functools.wraps(fn)
        async def wrapper(*args: object, **kwargs: object) -> dict[str, object]:
            try:
                result = fn(*args, **kwargs)
                if inspect.isawaitable(result):
                    return await result  # async tool bodies (none today)
                return result
            except (
                queries.UnknownMetric,
                queries.UnknownWorkout,
                queries.InvalidParameter,
                ValueError,
            ) as exc:
                raise ToolError(str(exc)) from exc

        return wrapper

    @server.tool(
        name="query_day",
        description="Daily rollup summary for one KST day (sum/avg/min/max/count per metric).",
        structured_output=True,
    )
    def tool_query_day(
        date: Annotated[str, Field(description="KST day, YYYY-MM-DD (e.g. 2026-09-08)")],
        metric: ALL_METRICS = "",
    ) -> dict[str, object]:
        result = queries.query_day(date, metric=metric, pool=pool)
        # Single-metric calls flatten the (single-entry) metrics list so the
        # day object itself carries sum/avg/min/max/count (the common agent
        # shape); all-metric calls keep the list under "metrics".
        if metric and result["count"] == 1:
            flat = dict(result["metrics"][0])
            flat["day"] = result["day"]
            return flat
        return result

    @server.tool(
        name="get_metric_series",
        description=(
            "Daily series for one metric over an inclusive KST-day range "
            "(rollup-level, fast)."
        ),
        structured_output=True,
    )
    def tool_get_metric_series(
        metric: str,
        frm: Annotated[
            str,
            Field(
                validation_alias="from",
                serialization_alias="from",
                description="start day YYYY-MM-DD",
            ),
        ] = "",
        to: Annotated[str, Field(description="end day YYYY-MM-DD")] = "",
    ) -> dict[str, object]:
        return queries.get_metric_series(metric, frm, to, pool=pool)

    @server.tool(
        name="list_metrics",
        description="Metric catalog: names, units, cumulative flags, coverage (first/last day).",
        structured_output=True,
    )
    def tool_list_metrics() -> dict[str, object]:
        return queries.list_metrics(pool=pool)

    @server.tool(
        name="list_workouts",
        description=(
            "Workouts in an inclusive KST-day range (newest first; "
            "hr/route point counts included)."
        ),
        structured_output=True,
    )
    def tool_list_workouts(
        frm: Annotated[
            str,
            Field(
                validation_alias="from",
                serialization_alias="from",
                description="start day YYYY-MM-DD",
            ),
        ] = "",
        to: Annotated[str, Field(description="end day YYYY-MM-DD")] = "",
    ) -> dict[str, object]:
        return queries.list_workouts(frm, to, pool=pool)

    @server.tool(
        name="get_workout_detail",
        description="One workout with its heart-rate series and GPS route (ordered by timestamp).",
        structured_output=True,
    )
    def tool_get_workout_detail(
        id: Annotated[str, Field(description="workout UUID (see list_workouts)")]
    ) -> dict[str, object]:
        return queries.get_workout_detail(id, pool=pool)

    @server.tool(
        name="get_trends",
        description=(
            "Trailing N-day trend for one metric ending at its last day "
            "(cumulative metrics aggregate with sum, others with avg)."
        ),
        structured_output=True,
    )
    def tool_get_trends(
        metric: str,
        window: Annotated[int, Field(description="window in days (1..365)")] = 7,
    ) -> dict[str, object]:
        return queries.get_trends(metric, window, pool=pool)

    @server.tool(
        name="freshness_check",
        description=(
            "Per-metric latest days, consensus latest_complete_day "
            "(median-based), stale list."
        ),
        structured_output=True,
    )
    def tool_freshness_check() -> dict[str, object]:
        return queries.freshness_check(pool=pool)

    @server.tool(
        name="raw_samples",
        description=(
            "Raw sample rows (paginated): limit default 1000 max 10000; "
            "next_offset None = last page."
        ),
        structured_output=True,
    )
    def tool_raw_samples(
        metric: ALL_METRICS = "",
        frm: Annotated[
            str,
            Field(
                validation_alias="from",
                serialization_alias="from",
                description="start day YYYY-MM-DD",
            ),
        ] = "",
        to: Annotated[str, Field(description="end day YYYY-MM-DD")] = "",
        limit: Annotated[
            int, Field(description="page size (default 1000, max 10000)")
        ] = queries.RAW_DEFAULT_LIMIT,
        offset: Annotated[int, Field(description="0-based page offset")] = 0,
    ) -> dict[str, object]:
        return queries.raw_samples(
            metric=metric, start=frm, end=to, limit=limit, offset=offset, pool=pool
        )

    # Wire the guarded bodies: replace the raw registrations with guarded
    # closures so UnknownMetric / UnknownWorkout / ValueError from the query
    # layer surface as client-visible ToolErrors (never masked). The raw
    # registrations are removed first (add_tool keeps the *existing* tool on
    # a duplicate name — remove_tool + add_tool performs the replacement).
    for name in (
        "query_day",
        "get_metric_series",
        "list_metrics",
        "list_workouts",
        "get_workout_detail",
        "get_trends",
        "freshness_check",
        "raw_samples",
    ):
        server.remove_tool(name)
    server.add_tool(guarded(tool_query_day), name="query_day")
    server.add_tool(guarded(tool_get_metric_series), name="get_metric_series")
    server.add_tool(guarded(tool_list_metrics), name="list_metrics")
    server.add_tool(guarded(tool_list_workouts), name="list_workouts")
    server.add_tool(guarded(tool_get_workout_detail), name="get_workout_detail")
    server.add_tool(guarded(tool_get_trends), name="get_trends")
    server.add_tool(guarded(tool_freshness_check), name="freshness_check")
    server.add_tool(guarded(tool_raw_samples), name="raw_samples")

    @server.resource(
        queries.RESOURCE_METRICS_URI,
        name="metrics",
        description="Metric catalog with units, cumulative flags, coverage (list_metrics payload).",
        mime_type="application/json",
    )
    def resource_metrics() -> dict[str, object]:
        return queries.list_metrics(pool=pool)

    @server.resource(
        queries.RESOURCE_FRESHNESS_URI,
        name="freshness",
        description="Latest complete KST day + per-metric staleness (freshness_check payload).",
        mime_type="application/json",
    )
    def resource_freshness() -> dict[str, object]:
        return queries.freshness_check(pool=pool)

    @server.resource(
        queries.RESOURCE_SCHEMA_URI,
        name="schema",
        description="Table/column summary of the health-db PostgreSQL schema.",
        mime_type="application/json",
    )
    def resource_schema() -> dict[str, object]:
        return queries.schema_resource()

    return server


def mcp_asgi_app(pool: object | None = None) -> Starlette:
    """Streamable-HTTP ASGI app for the MCP surface (Bearer-guarded).

    ``json_response=True`` (single JSON body per request — no SSE stream,
    proxy-friendly) and ``stateless_http=True`` (fresh transport per request,
    no session state) per research/mcp-python-sdk.md; both verified in-slice
    against the official SDK ``Client`` over real HTTP. Host ``127.0.0.1``
    is passed so the SDK's built-in localhost DNS-rebinding protection is
    configured with the matching allowlist (the uvicorn bind host is
    ``config.HOST``/``MCP_HOST`` and may be 0.0.0.0 on the host).
    """
    inner = build_mcp_server(pool).streamable_http_app(
        streamable_http_path=MCP_PATH,
        json_response=True,
        stateless_http=True,
        host="127.0.0.1",
    )
    inner.add_middleware(BearerAuthMiddleware)
    return inner


def serve() -> None:
    """Serve the MCP app on its own uvicorn port (``uv run health-mcp``).

    Reads ``config.load()`` (secrets, HOST) plus ``MCP_HOST``/``MCP_PORT``;
    the ingest service keeps its own process/port.
    """
    import uvicorn

    config.load()
    app_obj = mcp_asgi_app()
    uvicorn.run(app_obj, host=mcp_host(), port=mcp_port())


def main() -> None:
    """Console-script alias — pyproject wires ``health-mcp`` here."""
    serve()


def app() -> Starlette:
    """Module-level ASGI app factory for ``uvicorn health_db.mcp_server:app --factory``.

    Kept lazy (no import-time server build) so ``import health_db.mcp_server``
    stays cheap and the module remains importable without a DB — the docker
    entrypoint resolves this target.
    """
    return mcp_asgi_app()
