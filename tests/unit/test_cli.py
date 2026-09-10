"""Unit tests — health_db.cli: thin CLI over the shared query layer.

No DB, no docker: handlers are exercised with a :class:`FakeQueries` query
layer (acceptance contract for slice-2-cli) and :class:`FakePool` for the
pool-resolution paths in ``main``. Assertions cover observable behavior:
exit codes, exact JSON on stdout, diagnostics on stderr, argument
propagation to the query layer, actionable error text, and help surfaces.
"""

from __future__ import annotations

import io
import json

import pytest

import health_db.cli as cli
from health_db.cli import (
    COMMAND_OPTIONS,
    COMMANDS,
    ERR_DB_UNAVAILABLE,
    build_parser,
    emit_json,
    main,
    run_metrics,
    run_query_day,
    run_raw,
    run_routes,
    run_trends,
    run_workouts,
)

# ---------------------------------------------------------------------------
# Fakes: query layer + pool (no real DB anywhere in this file)
# ---------------------------------------------------------------------------


class FakeQueries:
    """Records calls and returns canned payloads — the fake query layer."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def _record(self, name: str, args: tuple[object, ...], kwargs: dict[str, object]) -> dict:
        self.calls.append((name, args, kwargs))
        return {"called": name, "args": list(args), **kwargs}

    def query_day(self, day, metric="", pool=None):  # noqa: ANN001
        return self._record("query_day", (day,), {"metric": metric, "pool": pool})

    def list_metrics(self, pool=None):  # noqa: ANN001
        return self._record("list_metrics", (), {"pool": pool})

    def list_workouts(self, start, end, pool=None):  # noqa: ANN001
        return self._record("list_workouts", (start, end), {"pool": pool})

    def get_trends(self, metric, window, pool=None):  # noqa: ANN001
        return self._record("get_trends", (metric, window), {"pool": pool})

    def freshness_check(self, pool=None):  # noqa: ANN001
        return self._record("freshness_check", (), {"pool": pool})

    def raw_samples(self, metric="", start="", end="", limit=1000, offset=0, pool=None):  # noqa: ANN001
        return self._record(
            "raw_samples",
            (),
            {
                "metric": metric,
                "start": start,
                "end": end,
                "limit": limit,
                "offset": offset,
                "pool": pool,
            },
        )

    def get_workout_detail(self, workout_id, pool=None):  # noqa: ANN001
        return self._record("get_workout_detail", (workout_id,), {"pool": pool})


@pytest.fixture()
def fq() -> FakeQueries:
    return FakeQueries()


@pytest.fixture()
def out(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    """Capture emit_json stdout (replaces the module's stdout binding)."""
    stream = io.StringIO()
    monkeypatch.setattr(cli, "sys_stdout", stream, raising=False)
    return stream


@pytest.fixture()
def err(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    """Capture emit_progress stderr (replaces the module's stderr binding)."""
    stream = io.StringIO()
    monkeypatch.setattr(cli, "sys_stderr", stream, raising=False)
    return stream


def parsed_out(stream: io.StringIO) -> object:
    """The single JSON document written to the captured stdout."""
    return json.loads(stream.getvalue())


# ---------------------------------------------------------------------------
# Handlers: arg parsing + query-layer funneling + stdout JSON shape
# ---------------------------------------------------------------------------


class TestHandlers:
    def test_query_day_passes_date_and_metric(self, fq, out):
        rc = run_query_day(["2026-09-08", "--metric", "step_count"], q=fq, pool=1)
        assert rc == 0
        assert fq.calls == [("query_day", ("2026-09-08",), {"metric": "step_count", "pool": 1})]
        assert parsed_out(out) == {
            "called": "query_day",
            "args": ["2026-09-08"],
            "metric": "step_count",
            "pool": 1,
        }

    def test_query_day_defaults_metric_empty(self, fq, out):
        run_query_day(["2026-09-08"], q=fq, pool=1)
        assert fq.calls[0][2]["metric"] == ""

    def test_workouts_passes_from_to(self, fq, out):
        rc = run_workouts(["--from", "2026-09-01", "--to", "2026-09-08"], q=fq, pool=1)
        assert rc == 0
        assert fq.calls == [("list_workouts", ("2026-09-01", "2026-09-08"), {"pool": 1})]
        assert parsed_out(out)["args"] == ["2026-09-01", "2026-09-08"]

    def test_workouts_defaults_empty(self, fq, out):
        run_workouts([], q=fq, pool=1)
        assert fq.calls[0][1] == ("", "")

    def test_trends_passes_metric_and_window(self, fq, out):
        run_trends(["step_count", "--window", "30"], q=fq, pool=1)
        assert fq.calls == [("get_trends", ("step_count", 30), {"pool": 1})]

    def test_trends_window_defaults_to_7(self, fq, out):
        run_trends(["step_count"], q=fq, pool=1)
        assert fq.calls[0][1] == ("step_count", 7)

    def test_metrics_no_options(self, fq, out):
        rc = run_metrics([], q=fq, pool=1)
        assert rc == 0
        assert fq.calls == [("list_metrics", (), {"pool": 1})]
        assert parsed_out(out) == {"called": "list_metrics", "args": [], "pool": 1}

    def test_raw_passes_pagination(self, fq, out):
        run_raw(
            [
                "--metric",
                "step_count",
                "--from",
                "2026-09-01",
                "--to",
                "2026-09-08",
                "--limit",
                "10",
                "--offset",
                "20",
            ],
            q=fq,
            pool=1,
        )
        recorded = fq.calls[0][2]
        assert recorded == {
            "metric": "step_count",
            "start": "2026-09-01",
            "end": "2026-09-08",
            "limit": 10,
            "offset": 20,
            "pool": 1,
        }

    def test_raw_defaults(self, fq, out):
        run_raw([], q=fq, pool=1)
        recorded = fq.calls[0][2]
        assert recorded["limit"] == 1000
        assert recorded["offset"] == 0
        assert recorded["metric"] == ""
        assert recorded["start"] == ""
        assert recorded["end"] == ""

    def test_routes_passes_uuid(self, fq, out):
        run_routes(["abc-123"], q=fq, pool=1)
        assert fq.calls == [("get_workout_detail", ("abc-123",), {"pool": 1})]
        assert parsed_out(out)["args"] == ["abc-123"]

    def test_freshness_no_options(self, fq, out):
        rc = COMMANDS["freshness"]([], q=fq, pool=1)
        assert rc == 0
        assert fq.calls == [("freshness_check", (), {"pool": 1})]


class TestHandlerUsageErrors:
    """Bad argv → argparse SystemExit(2); ``--help`` → SystemExit(0) on stdout."""

    def test_query_day_requires_date(self, capsys):
        with pytest.raises(SystemExit) as exc:
            run_query_day([], q=FakeQueries(), pool=1)
        assert exc.value.code == 2
        assert "date" in capsys.readouterr().err  # usage text on real stderr

    def test_query_day_rejects_unknown_flag(self, capsys):
        with pytest.raises(SystemExit) as exc:
            run_query_day(["2026-09-08", "--nope"], q=FakeQueries(), pool=1)
        assert exc.value.code == 2
        assert "--nope" in capsys.readouterr().err

    def test_trends_window_must_be_int(self, capsys):
        with pytest.raises(SystemExit) as exc:
            run_trends(["step_count", "--window", "abc"], q=FakeQueries(), pool=1)
        assert exc.value.code == 2
        assert "window" in capsys.readouterr().err

    def test_routes_requires_id(self, capsys):
        with pytest.raises(SystemExit) as exc:
            run_routes([], q=FakeQueries(), pool=1)
        assert exc.value.code == 2
        assert "id" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Error surfacing: actionable text on stderr, exit 1 (no traceback)
# ---------------------------------------------------------------------------


class TestMainErrorSurfacing:
    def _patch_handler(self, layer_type):
        """Swap the query-day COMMANDS entry for one backed by ``layer_type``.

        The shim keeps main's call contract (positional argv + pool kwarg);
        the fake layer rides in as the shim's ``q`` default — main never
        passes ``q``, so the fake layer is what the handler actually uses.
        """
        saved = COMMANDS["query-day"]

        def shim(argv, q=layer_type, pool=None):
            return run_query_day(argv, q=q, pool=pool)

        COMMANDS["query-day"] = shim
        return saved

    def test_unknown_metric_prints_actionable_text_exit_1(self, monkeypatch, out, err):
        from health_db.queries import UnknownMetric

        exc = UnknownMetric("nope_metric")

        class _Layer:
            @staticmethod
            def query_day(day, metric="", pool=None):  # noqa: ANN001
                raise exc

        saved = self._patch_handler(_Layer)
        try:
            monkeypatch.setattr(cli, "_pool_or_none", lambda: 1)
            rc = main(["query-day", "2026-09-08"])
        finally:
            COMMANDS["query-day"] = saved
        assert rc == 1
        assert "nope_metric" in err.getvalue()
        assert "list_metrics" in err.getvalue()
        assert out.getvalue() == ""  # no JSON on failure

    def test_invalid_date_prints_format_hint_exit_1(self, monkeypatch, out, err):
        exc = cli.queries.InvalidParameter("invalid date 'x' — use YYYY-MM-DD")

        class _Layer:
            @staticmethod
            def query_day(day, metric="", pool=None):  # noqa: ANN001
                raise exc

        saved = self._patch_handler(_Layer)
        try:
            monkeypatch.setattr(cli, "_pool_or_none", lambda: 1)
            rc = main(["query-day", "x"])
        finally:
            COMMANDS["query-day"] = saved
        assert rc == 1
        assert "YYYY-MM-DD" in err.getvalue()

    def test_generic_exception_never_leaks_details(self, monkeypatch, out, err):
        """DB failure: generic secret-free message only (iron law 1)."""

        class _Layer:
            @staticmethod
            def query_day(day, metric="", pool=None):  # noqa: ANN001
                raise RuntimeError("password=sekret host=db.internal boom")

        saved = self._patch_handler(_Layer)
        try:
            monkeypatch.setattr(cli, "_pool_or_none", lambda: 1)
            rc = main(["query-day", "2026-09-08"])
        finally:
            COMMANDS["query-day"] = saved
        assert rc == 1
        text = err.getvalue()
        assert "query failed" in text
        assert "sekret" not in text
        assert "db.internal" not in text

    def test_no_pool_configured_exit_1(self, monkeypatch, out, err):
        monkeypatch.setattr(cli, "_pool_or_none", lambda: None)
        rc = main(["query-day", "2026-09-08"])
        assert rc == 1
        assert ERR_DB_UNAVAILABLE in err.getvalue()
        assert out.getvalue() == ""

    def test_missing_command_exit_2_with_help(self, err):
        # explicit empty argv (embedded invocation) → usage + hint on stderr
        rc = main([])
        assert rc == 2
        assert "query-day" in err.getvalue()  # help text (routed to stderr)
        assert "no command given" in err.getvalue()

    def test_unknown_command_is_argparse_error(self):
        with pytest.raises(SystemExit) as exc:
            main(["nope"])
        assert exc.value.code == 2


# ---------------------------------------------------------------------------
# Output contract: stdout = single JSON line (sort_keys, parseable)
# ---------------------------------------------------------------------------


class TestEmitJson:
    def test_json_sorted_and_parseable(self):
        stream = io.StringIO()
        emit_json({"b": 1, "a": [2, 1]}, stream=stream)
        text = stream.getvalue()
        assert text.endswith("\n")
        assert json.loads(text) == {"b": 1, "a": [2, 1]}
        assert text.index('"a"') < text.index('"b"')  # sorted keys

    def test_non_serializable_fails_loudly(self):
        stream = io.StringIO()
        with pytest.raises(TypeError):
            emit_json({"bad": object()}, stream=stream)


# ---------------------------------------------------------------------------
# Command registry + parser wiring (the spec's command surface)
# ---------------------------------------------------------------------------


class TestCommandSurface:
    def test_exactly_the_seven_spec_commands(self):
        assert sorted(COMMANDS) == [
            "freshness",
            "metrics",
            "query-day",
            "raw",
            "routes",
            "trends",
            "workouts",
        ]

    def test_every_command_has_option_specs(self):
        assert set(COMMAND_OPTIONS) == set(COMMANDS)

    def test_top_level_help_lists_all_commands(self, capsys):
        with pytest.raises(SystemExit) as exc:
            build_parser().parse_args(["--help"])
        assert exc.value.code == 0
        text = capsys.readouterr().out
        for name in COMMANDS:
            assert name in text

    def test_subcommand_help_shows_options_without_running(self, fq, out, capsys):
        # ``--help`` must NOT reach the query layer (argparse exits first).
        with pytest.raises(SystemExit) as exc:
            run_query_day(["--help"], q=fq, pool=1)
        assert exc.value.code == 0
        assert fq.calls == []  # query layer never invoked
        text = capsys.readouterr().out
        assert "--metric" in text
        assert "YYYY-MM-DD" in text

    def test_workouts_help_shows_from_to(self, capsys):
        with pytest.raises(SystemExit) as exc:
            run_workouts(["--help"], q=FakeQueries(), pool=1)
        assert exc.value.code == 0
        text = capsys.readouterr().out
        assert "--from" in text and "--to" in text

    def test_raw_help_shows_pagination_flags(self, capsys):
        with pytest.raises(SystemExit) as exc:
            run_raw(["--help"], q=FakeQueries(), pool=1)
        assert exc.value.code == 0
        text = capsys.readouterr().out
        for flag in ("--metric", "--limit", "--offset", "--from", "--to"):
            assert flag in text

    def test_main_dispatches_to_handler_with_remaining_argv(self, monkeypatch, out, err):
        """Full dispatch: top-level parse → handler with the command's argv."""
        captured: dict[str, object] = {}

        def fake_handler(argv, q=None, pool=None):  # noqa: ANN001
            captured["argv"] = argv
            captured["pool"] = pool
            emit_json({"ok": True})
            return 0

        saved = dict(COMMANDS)
        COMMANDS["metrics"] = fake_handler
        try:
            monkeypatch.setattr(cli, "_pool_or_none", lambda: "pool-sentinel")
            rc = main(["metrics"])
        finally:
            COMMANDS.clear()
            COMMANDS.update(saved)
        assert rc == 0
        assert captured["argv"] == []
        assert captured["pool"] == "pool-sentinel"
        assert parsed_out(out) == {"ok": True}


# ---------------------------------------------------------------------------
# Pool resolution gate: never builds against ambient defaults
# ---------------------------------------------------------------------------


class TestPoolResolution:
    def test_main_refuses_when_no_pool_and_no_env(self, monkeypatch, err):
        from health_db.db import pool as db_pool

        monkeypatch.setattr(db_pool, "existing_pool", lambda: None)
        monkeypatch.setattr(db_pool, "has_explicit_env", lambda environ=None: False)
        rc = main(["query-day", "2026-09-08"])
        assert rc == 1
        assert "database unavailable or not configured" in err.getvalue()

    def test_main_resolves_existing_singleton(self, monkeypatch, out):
        from health_db.db import pool as db_pool

        class _P:
            pass

        monkeypatch.setattr(db_pool, "existing_pool", lambda: _P())
        seen: dict[str, object] = {}

        def fake_handler(argv, q=None, pool=None):  # noqa: ANN001
            seen["pool"] = pool
            return 0

        saved = dict(COMMANDS)
        COMMANDS["metrics"] = fake_handler
        try:
            rc = main(["metrics"])
        finally:
            COMMANDS.clear()
            COMMANDS.update(saved)
        assert rc == 0
        assert type(seen["pool"]).__name__ == "_P"


class TestThinWrapper:
    def test_handlers_default_to_real_queries_module(self):
        """The default ``q`` IS health_db.queries — no SQL duplication layer."""
        import inspect

        for name, handler in COMMANDS.items():
            default_q = inspect.signature(handler).parameters["q"].default
            assert default_q is cli.queries, name
            source = inspect.getsource(handler)
            assert "FROM " not in source and "SELECT" not in source, name
