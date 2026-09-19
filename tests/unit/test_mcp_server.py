"""Unit tests — health_db.mcp_server: bearer auth + tool/resource registration.

No docker, no DB: the pool seam is a stub or never reached (the ``schema``
resource needs no pool). The full surface is covered by the slice-2
acceptance ``mcp`` fixture (real streamable-http round-trips against a
disposable postgres container).
"""

from __future__ import annotations

import asyncio

import health_db.mcp_server as mcp_server
from health_db.mcp_server import bearer_ok, build_mcp_server, mcp_port

# ---------------------------------------------------------------------------
# bearer_ok: constant-time Bearer validation (fail-closed)
# ---------------------------------------------------------------------------


class TestBearerOk:
    def test_valid_token_passes(self):
        assert bearer_ok("Bearer sekret", "sekret") is True

    def test_case_insensitive_scheme(self):
        assert bearer_ok("bearer sekret", "sekret") is True

    def test_missing_header(self):
        assert bearer_ok(None, "sekret") is False

    def test_missing_scheme(self):
        assert bearer_ok("sekret", "sekret") is False

    def test_wrong_scheme(self):
        assert bearer_ok("Basic sekret", "sekret") is False

    def test_wrong_token(self):
        assert bearer_ok("Bearer nope", "sekret") is False

    def test_empty_token(self):
        assert bearer_ok("Bearer ", "sekret") is False

    def test_fail_closed_when_secret_unconfigured(self):
        assert bearer_ok("Bearer anything", None) is False
        assert bearer_ok("Bearer anything", "") is False

    def test_prefix_collision_rejected(self):
        assert bearer_ok("Bearer sekret-extra", "sekret") is False


# ---------------------------------------------------------------------------
# env helpers
# ---------------------------------------------------------------------------


class TestMcpPort:
    def test_default(self):
        assert mcp_port({}) == 8100

    def test_override(self):
        assert mcp_port({"MCP_PORT": "9100"}) == 9100

    def test_invalid_falls_back(self):
        assert mcp_port({"MCP_PORT": "abc"}) == 8100

    def test_blank_falls_back(self):
        assert mcp_port({"MCP_PORT": "  "}) == 8100


class TestMcpAllowedHosts:
    def test_defaults_are_localhost_only(self):
        assert mcp_server.mcp_allowed_hosts({}) == [
            "127.0.0.1:*",
            "localhost:*",
            "[::1]:*",
        ]

    def test_extra_hosts_are_appended(self):
        hosts = mcp_server.mcp_allowed_hosts({"MCP_ALLOWED_HOSTS": "healthbox.example:8443"})
        assert hosts[-1] == "healthbox.example:8443"
        assert "127.0.0.1:*" in hosts

    def test_multiple_hosts_split_and_trimmed(self):
        hosts = mcp_server.mcp_allowed_hosts({"MCP_ALLOWED_HOSTS": "healthbox.example:8443, server.local ,,"})
        assert hosts[-2:] == ["healthbox.example:8443", "server.local"]

    def test_blank_entries_dropped(self):
        assert mcp_server.mcp_allowed_hosts({"MCP_ALLOWED_HOSTS": " , "}) == [
            "127.0.0.1:*",
            "localhost:*",
            "[::1]:*",
        ]


# ---------------------------------------------------------------------------
# build_mcp_server: 8 tools + 3 resources, guarded errors
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, raise_on_execute: bool = True) -> None:
        self._raise = raise_on_execute

    def execute(self, sql, params=()):  # noqa: ANN001
        if self._raise:
            raise RuntimeError("fake pool: no sql")
        return self

    def fetchall(self):  # noqa: ANN201
        return []

    def __enter__(self):  # noqa: ANN201
        return self

    def __exit__(self, *args: object) -> bool:
        return False


class _FakeConn:
    def __enter__(self):  # noqa: ANN201
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def cursor(self, row_factory=None):  # noqa: ANN001, ARG002
        return _FakeCursor()


class _FakePool:
    def connection(self):  # noqa: ANN201
        return _FakeConn()


class _FakeConn(_FakePool):
    pass


def _tool_names(server) -> list[str]:
    return sorted(t.name for t in server._tool_manager.list_tools())


class TestBuildMcpServer:
    def test_registers_exactly_the_11_spec_tools(self):
        """The original 8 tools (unchanged schemas) + run
        2026-09-17-normalizer-audit §7's three lane tools."""
        server = build_mcp_server(pool=_FakePool())
        names = _tool_names(server)
        assert names == [
            "freshness_check",
            "get_ecg_detail",
            "get_metric_series",
            "get_trends",
            "get_workout_detail",
            "list_ecgs",
            "list_heart_notifications",
            "list_metrics",
            "list_workouts",
            "query_day",
            "raw_samples",
        ]

    def test_from_alias_published_in_schema(self):
        server = build_mcp_server(pool=_FakePool())
        tool = next(
            t for t in server._tool_manager.list_tools() if t.name == "get_metric_series"
        )
        assert "from" in tool.parameters["properties"]
        assert "frm" not in tool.parameters["properties"]

    def test_raw_samples_limit_bounds_documented(self):
        server = build_mcp_server(pool=_FakePool())
        tool = next(
            t for t in server._tool_manager.list_tools() if t.name == "raw_samples"
        )
        props = tool.parameters["properties"]
        assert "limit" in props and "offset" in props

    def test_three_resources_registered(self):
        server = build_mcp_server(pool=_FakePool())
        uris = sorted(str(r.uri) for r in server._resource_manager.list_resources())
        assert uris == ["health://freshness", "health://metrics", "health://schema"]


# ---------------------------------------------------------------------------
# Guarded tools: actionable errors surface through the tool result
# ---------------------------------------------------------------------------


class _ErrConn:
    """Connection whose cursor raises UnknownMetric-shaped misses (empty)."""

    def __enter__(self):  # noqa: ANN201
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def cursor(self, row_factory=None):  # noqa: ANN001

        cur = _EmptyCursor()
        return cur


class _EmptyCursor:
    def execute(self, sql, params=()):  # noqa: ANN001, ARG002
        return None

    def fetchall(self):  # noqa: ANN201
        return []

    def __enter__(self):  # noqa: ANN201
        return self

    def __exit__(self, *args: object) -> bool:
        return False


class _EmptyPool:
    def connection(self):  # noqa: ANN201
        return _ErrConn()


class TestGuardedToolErrors:
    def _call(self, name: str, arguments: dict[str, object]) -> object:
        from mcp import Client

        server = build_mcp_server(pool=_EmptyPool())

        async def run():  # noqa: ANN202
            async with Client(server) as client:
                return await client.call_tool(name, arguments)

        return asyncio.run(run())

    def test_unknown_metric_error_text_is_actionable(self):
        result = self._call(
            "get_metric_series",
            {"metric": "nope_metric", "from": "2026-09-01", "to": "2026-09-08"},
        )
        assert result.is_error is True
        text = result.content[0].text
        assert "nope_metric" in text
        assert "list_metrics" in text

    def test_invalid_date_error_names_expected_format(self):
        result = self._call("query_day", {"date": "2026-13-99"})
        assert result.is_error is True
        assert "YYYY-MM-DD" in result.content[0].text

    def test_trends_window_must_be_positive(self):
        result = self._call("get_trends", {"metric": "x", "window": 0})
        assert result.is_error is True
        assert "window" in result.content[0].text


# ---------------------------------------------------------------------------
# BearerAuthMiddleware: ASGI-level 401 before any dispatch
# ---------------------------------------------------------------------------


class TestBearerMiddleware:
    def test_missing_token_401(self):
        from starlette.testclient import TestClient

        import health_db.config as config

        saved = config.INGEST_SECRET
        config.INGEST_SECRET = "tok-123"
        try:
            from mcp.server.mcpserver import MCPServer

            inner = MCPServer("t").streamable_http_app(
                streamable_http_path="/mcp",
                json_response=True,
                stateless_http=True,
                host="127.0.0.1",
            )
            inner.add_middleware(mcp_server.BearerAuthMiddleware)
            client = TestClient(inner)
            res = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
            assert res.status_code == 401
        finally:
            config.INGEST_SECRET = saved

    def test_fail_closed_when_secret_none(self):
        from starlette.testclient import TestClient

        import health_db.config as config

        saved = config.INGEST_SECRET
        config.INGEST_SECRET = None
        try:
            from mcp.server.mcpserver import MCPServer

            inner = MCPServer("t").streamable_http_app(
                streamable_http_path="/mcp",
                json_response=True,
                stateless_http=True,
                host="127.0.0.1",
            )
            inner.add_middleware(mcp_server.BearerAuthMiddleware)
            client = TestClient(inner)
            res = client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                headers={"Authorization": "Bearer tok-123"},
            )
            assert res.status_code == 401
        finally:
            config.INGEST_SECRET = saved

    def test_non_http_scope_passes_through(self):
        calls: list[object] = []

        async def app(scope, receive, send):  # noqa: ANN001
            calls.append(scope["type"])

        mw = mcp_server.BearerAuthMiddleware(app)
        asyncio.run(mw({"type": "lifespan"}, None, None))
        assert calls == ["lifespan"]


# ---------------------------------------------------------------------------
# mcp_asgi_app wiring
# ---------------------------------------------------------------------------


class TestMcpAsgiApp:
    def test_app_builds_with_starlette(self):
        app = mcp_server.mcp_asgi_app(pool=_FakePool())
        assert app is not None

    def test_mcp_path_constant(self):
        assert mcp_server.MCP_PATH == "/mcp"


class TestMcpHostHeader:
    """DNS-rebinding allowlist: non-localhost Hosts must reach the MCP layer.

    Regression for reverse-proxy deployments: the old ``host="127.0.0.1"``
    shorthand installed a localhost-only allowlist, so a proxied
    ``Host: healthbox.example:8443`` was rejected with 421 *after* a valid Bearer
    check. TestClient's default Host is ``testserver``, which stands in for
    that proxy hostname; these tests send a valid token so the Bearer
    middleware passes and the SDK's host validation is what decides.
    """

    TOKEN = "tok-host-test"

    def _post(self, app, host: str) -> int:
        from starlette.testclient import TestClient

        # Context-managed so the MCP session manager's lifespan runs.
        with TestClient(app) as client:
            res = client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                headers={
                    "Host": host,
                    "Authorization": f"Bearer {self.TOKEN}",
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
            return res.status_code

    def test_unlisted_host_rejected_with_421(self, monkeypatch):
        import health_db.config as config

        monkeypatch.setattr(config, "INGEST_SECRET", self.TOKEN)
        app = mcp_server.mcp_asgi_app(pool=_FakePool())
        assert self._post(app, "testserver") == 421

    def test_allowlisted_host_passes_host_check(self, monkeypatch):
        import health_db.config as config

        monkeypatch.setattr(config, "INGEST_SECRET", self.TOKEN)
        monkeypatch.setenv("MCP_ALLOWED_HOSTS", "testserver")
        app = mcp_server.mcp_asgi_app(pool=_FakePool())
        status = self._post(app, "testserver")
        # 401 would mean auth failed; 421 would mean the host was rejected.
        assert status not in (401, 421)

    def test_localhost_host_accepted_by_default(self, monkeypatch):
        import health_db.config as config

        monkeypatch.setattr(config, "INGEST_SECRET", self.TOKEN)
        app = mcp_server.mcp_asgi_app(pool=_FakePool())
        assert self._post(app, "localhost:8100") not in (401, 421)
