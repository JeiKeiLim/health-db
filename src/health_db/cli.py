"""Thin ``health`` CLI over the shared query layer (job slice-2-cli).

Spec §"CLI wrapper (dev testing — slice 2)": ``uv run health <command>``
mirrors the core MCP tools (``query-day``, ``metrics``, ``workouts``,
``trends``, ``freshness``, ``raw``, ``routes``; run
2026-09-17-normalizer-audit §7 adds ``ecgs``, ``ecg-detail``, ``hrn``; run
2026-09-28-mcp-analysis-parity adds ``sleep``) as a
*dev testing* surface —
"not the product surface; thin shared query-layer". Accordingly this module
contains **no SQL and no business logic**: every command funnels through
:mod:`health_db.queries` (the decomposition interface contract: "queries —
the single read surface shared by MCP tools + CLI + resources"), exactly the
functions the MCP tools call.

Output contract (replay.py / migrations.py CLI parity):

- **stdout carries only JSON** — one ``json.dumps`` per successful command,
  so output pipes cleanly into ``jq``. Diagnostics ("resolving database
  pool...", per-error notes) go to **stderr**.
- Exit codes: ``0`` on success, ``2`` on argparse-level usage errors or a
  missing command (argparse convention), ``1`` for query errors (unknown
  metric, invalid date/range/window — the actionable ``queries`` exception
  text is printed verbatim to stderr) and for database unavailability (a
  generic secret-free message, iron law 1 — psycopg exception text can
  embed connection settings and is never echoed).

Structure: one option-spec table (:data:`COMMAND_OPTIONS`) drives both the
per-command parsers (used by handlers) and the top-level subparsers (used by
``--help``), so parse behavior and help text can never drift apart. Pool
resolution mirrors the health-probe gate: the CLI only *resolves* a pool
(materialized singleton → explicit-env singleton) and never builds one on
ambient defaults — with nothing configured, every command exits 1 with the
actionable message. Unit tests run handlers directly with a fake query
layer (``q=fake``), so no DB is needed for CLI arg/JSON-shape tests
(acceptance contract).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import TYPE_CHECKING

import health_db.queries as queries

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool

# Live stream bindings (module attributes so tests can reseat them; the
# indirection mirrors health_db.config's live-attribute pattern).
sys_stdout: object = sys.stdout
sys_stderr: object = sys.stderr

CLI_PROG = "health"

# --- stderr progress strings (secret-free, static) ---
PROG_PREFIX = CLI_PROG
ERR_DB_UNAVAILABLE = (
    "database unavailable or not configured; set PSQL_* env (see .env.example) and retry"
)
ERR_QUERY_FAILED = "query failed"


class _JsonEncoder(json.JSONEncoder):
    """Pass-through for queries output (already JSON-ready primitives).

    A non-JSON-serializable value escaping the query layer is a bug, not a
    formatting concern — the encoder *fails loudly* (no silent ``str``
    coercion that would mask contract violations).
    """


def emit_json(payload: object, stream: object | None = None) -> None:
    """Write one JSON document + newline to stdout (test-injectable stream).

    ``sort_keys=True`` keeps output byte-stable across runs (parity with
    health-replay's summary line) so diff-based smoke checks stay reliable.
    """
    target = sys_stdout if stream is None else stream
    print(json.dumps(payload, sort_keys=True, cls=_JsonEncoder), file=target)


def emit_progress(line: str) -> None:
    """Diagnostics go to stderr; stdout stays pure JSON."""
    print(f"{PROG_PREFIX}: {line}", file=sys_stderr)


# ---------------------------------------------------------------------------
# Command option specs: one table drives BOTH the top-level subparsers (so
# ``health <command> --help`` shows the real options) and the handlers (so
# the parse behavior matches the help). Each entry: (flags, argparse kwargs).
# ---------------------------------------------------------------------------

OPT_METRIC = (
    ("--metric",),
    {"default": "", "help": "restrict to one metric name (default: all metrics)"},
)
OPT_FROM = (("--from",), {"dest": "frm", "default": "", "help": "start day YYYY-MM-DD"})
OPT_TO = (("--to",), {"default": "", "help": "end day YYYY-MM-DD"})

COMMAND_OPTIONS: dict[str, tuple[tuple[tuple[str, ...], dict[str, object]], ...]] = {
    "query-day": (
        (("date",), {"help": "KST day, YYYY-MM-DD (e.g. 2026-09-08)"}),
        OPT_METRIC,
    ),
    "metrics": (),
    "workouts": (OPT_FROM, OPT_TO),
    "trends": (
        (("metric",), {"help": "metric name (see the metrics command)"}),
        (
            ("--window",),
            {
                "type": int,
                "default": 7,
                "help": "window in days, 1..365 (default: 7)",
            },
        ),
    ),
    "freshness": (),
    "raw": (
        OPT_METRIC,
        OPT_FROM,
        OPT_TO,
        (("--limit",), {"type": int, "default": queries.RAW_DEFAULT_LIMIT}),
        (("--offset",), {"type": int, "default": 0}),
    ),
    "routes": (
        (("id",), {"help": "workout UUID (see the workouts command)"}),
        (
            ("--offset",),
            {"type": int, "default": 0, "help": "0-based route page offset"},
        ),
        (
            ("--limit",),
            {
                "type": int,
                "default": queries.ROUTE_DEFAULT_LIMIT,
                "help": "route page size, max 10000 (default: 1000)",
            },
        ),
    ),
    "ecgs": (
        OPT_FROM,
        OPT_TO,
        (("--limit",), {"type": int, "default": queries.ECG_HRN_DEFAULT_LIMIT}),
    ),
    "ecg-detail": (
        (("id",), {"help": "ECG session id (see the ecgs command)"}),
        (
            ("--voltage-offset",),
            {"type": int, "default": 0, "help": "0-based voltage page offset"},
        ),
        (
            ("--voltage-limit",),
            {
                "type": int,
                "default": queries.VOLTAGE_DEFAULT_LIMIT,
                "help": "voltage page size, max 10000 (default: 1000)",
            },
        ),
    ),
    "hrn": (
        OPT_FROM,
        OPT_TO,
        (("--limit",), {"type": int, "default": queries.ECG_HRN_DEFAULT_LIMIT}),
    ),
    "sleep": (
        OPT_FROM,
        OPT_TO,
        (
            ("--limit",),
            {
                "type": int,
                "default": queries.SLEEP_SESSION_DEFAULT_LIMIT,
                "help": "page size, max 500 (default: 100)",
            },
        ),
        (("--offset",), {"type": int, "default": 0, "help": "0-based page offset"}),
    ),
}


def _arg_parser(command: str) -> argparse.ArgumentParser:
    """Per-command parser built from :data:`COMMAND_OPTIONS`."""
    parser = argparse.ArgumentParser(
        prog=f"{CLI_PROG} {command}",
        description=COMMANDS[command].__doc__,  # type: ignore[attr-defined]
    )
    for flags, spec in COMMAND_OPTIONS[command]:
        parser.add_argument(*flags, **spec)  # type: ignore[arg-type]
    return parser


# ---------------------------------------------------------------------------
# Command handlers: argv -> queries call -> dict (the ONLY logic in this file)
# ---------------------------------------------------------------------------


def run_query_day(argv: list[str], *, q: object = queries, pool: object | None = None) -> int:
    """Daily rollup summary (sum/avg/min/max/count) for one KST day."""
    args = _arg_parser("query-day").parse_args(argv)
    result = q.query_day(args.date, metric=args.metric, pool=pool)  # type: ignore[attr-defined]
    emit_json(result)
    return 0


def run_metrics(argv: list[str], *, q: object = queries, pool: object | None = None) -> int:
    """Metric catalog: names, units, cumulative flags, coverage."""
    _arg_parser("metrics").parse_args(argv)  # no options (e.g. ``metrics --help``)
    result = q.list_metrics(pool=pool)  # type: ignore[attr-defined]
    emit_json(result)
    return 0


def run_workouts(argv: list[str], *, q: object = queries, pool: object | None = None) -> int:
    """Workouts in an inclusive KST-day range (newest first)."""
    args = _arg_parser("workouts").parse_args(argv)
    result = q.list_workouts(args.frm, args.to, pool=pool)  # type: ignore[attr-defined]
    emit_json(result)
    return 0


def run_trends(argv: list[str], *, q: object = queries, pool: object | None = None) -> int:
    """Trailing N-day trend for one metric (cumulative: sum, others: avg)."""
    args = _arg_parser("trends").parse_args(argv)
    result = q.get_trends(args.metric, args.window, pool=pool)  # type: ignore[attr-defined]
    emit_json(result)
    return 0


def run_freshness(argv: list[str], *, q: object = queries, pool: object | None = None) -> int:
    """Per-metric latest days, consensus latest_complete_day, latest_activity_day, stale list."""
    _arg_parser("freshness").parse_args(argv)
    result = q.freshness_check(pool=pool)  # type: ignore[attr-defined]
    emit_json(result)
    return 0


def run_raw(argv: list[str], *, q: object = queries, pool: object | None = None) -> int:
    """One page of raw sample rows (next_offset null on the last page)."""
    args = _arg_parser("raw").parse_args(argv)
    result = q.raw_samples(  # type: ignore[attr-defined]
        metric=args.metric,
        start=args.frm,
        end=args.to,
        limit=args.limit,
        offset=args.offset,
        pool=pool,
    )
    emit_json(result)
    return 0


def run_routes(argv: list[str], *, q: object = queries, pool: object | None = None) -> int:
    """One workout's GPS route points (paged, timestamp order)."""
    args = _arg_parser("routes").parse_args(argv)
    result = q.get_workout_detail(  # type: ignore[attr-defined]
        args.id, route_offset=args.offset, route_limit=args.limit, pool=pool
    )
    emit_json(result)
    return 0


def run_ecgs(argv: list[str], *, q: object = queries, pool: object | None = None) -> int:
    """ECG sessions in an inclusive KST-day range (newest first)."""
    args = _arg_parser("ecgs").parse_args(argv)
    result = q.list_ecgs(args.frm, args.to, limit=args.limit, pool=pool)  # type: ignore[attr-defined]
    emit_json(result)
    return 0


def run_ecg_detail(
    argv: list[str], *, q: object = queries, pool: object | None = None
) -> int:
    """One ECG session + a page of its voltage samples (seq order)."""
    args = _arg_parser("ecg-detail").parse_args(argv)
    result = q.get_ecg_detail(  # type: ignore[attr-defined]
        args.id, voltage_offset=args.voltage_offset, voltage_limit=args.voltage_limit,
        pool=pool,
    )
    emit_json(result)
    return 0


def run_hrn(argv: list[str], *, q: object = queries, pool: object | None = None) -> int:
    """Heart-rate-notification events with nested HR/HRV samples (newest first)."""
    args = _arg_parser("hrn").parse_args(argv)
    result = q.list_heart_notifications(args.frm, args.to, limit=args.limit, pool=pool)  # type: ignore[attr-defined]
    emit_json(result)
    return 0


def run_sleep(argv: list[str], *, q: object = queries, pool: object | None = None) -> int:
    """Sleep-session records with stages over an inclusive KST-day range."""
    args = _arg_parser("sleep").parse_args(argv)
    result = q.list_sleep_sessions(  # type: ignore[attr-defined]
        args.frm, args.to, limit=args.limit, offset=args.offset, pool=pool
    )
    emit_json(result)
    return 0


# Registry driving the top-level parser AND the test that pins the spec's
# command list (query-day, metrics, workouts, trends, freshness, raw, routes,
# + run 2026-09-17-normalizer-audit §7: ecgs, ecg-detail, hrn
# + run 2026-09-28-mcp-analysis-parity: sleep).
COMMANDS: dict[str, object] = {
    "query-day": run_query_day,
    "metrics": run_metrics,
    "workouts": run_workouts,
    "trends": run_trends,
    "freshness": run_freshness,
    "raw": run_raw,
    "routes": run_routes,
    "ecgs": run_ecgs,
    "ecg-detail": run_ecg_detail,
    "hrn": run_hrn,
    "sleep": run_sleep,
}


def build_parser() -> argparse.ArgumentParser:
    """Top-level ``health`` parser with one subparser per command.

    Subparser options come from the same :data:`COMMAND_OPTIONS` table the
    handlers parse with, so ``health <command> --help`` always matches the
    actual parse behavior.
    """
    parser = argparse.ArgumentParser(
        prog=CLI_PROG,
        description=(
            "Dev CLI over the health-db query layer (same functions the MCP "
            "tools call). Each command prints one JSON document to stdout; "
            "diagnostics go to stderr."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    for name, handler in COMMANDS.items():
        doc = handler.__doc__ or ""
        sub = subparsers.add_parser(name, help=doc, description=doc)
        for flags, spec in COMMAND_OPTIONS[name]:
            sub.add_argument(*flags, **spec)  # type: ignore[arg-type]
        # The subparser holds ONLY routing defaults; the handler re-parses
        # its argv slice with the per-command parser (never both).
        sub.set_defaults(_handler=handler)
    return parser


def _pool_or_none() -> ConnectionPool | None:
    """Resolve the pool the way ``queries`` production calls do.

    Uses the *live* ``db.pool`` module attributes (materialized singleton →
    explicit-env singleton, looked up at call time so test seams are
    honored). The CLI never builds a pool on ambient defaults: with nothing
    configured, commands fail with the actionable message instead of opening
    ``localhost:5432`` (parity with the health probe gate).
    """
    from health_db.db import pool as db_pool

    found = db_pool.existing_pool()
    if found is not None:
        return found
    if db_pool.has_explicit_env():
        return db_pool.get_pool()
    return None


def main(argv: list[str] | None = None) -> int:
    """``health`` entry point (pyproject console script).

    Dispatch is table-driven over :data:`COMMANDS` with the query module
    bound as the handlers' default keyword (``q=queries``) — unit tests
    substitute a fake query layer by calling the handlers directly with
    ``q=fake`` or by monkeypatching :data:`COMMANDS` entries.
    """
    if argv is None:
        argv = sys.argv[1:]
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        # No command: usage goes to stderr together with the hint (stdout
        # stays pure JSON for successful commands — an empty command is an
        # error, not output).
        parser.print_help(sys_stderr)
        emit_progress("no command given — pick one: " + ", ".join(sorted(COMMANDS)))
        return 2

    emit_progress("resolving database pool (PSQL_* env)...")
    try:
        pool = _pool_or_none()
        if pool is None:
            emit_progress(ERR_DB_UNAVAILABLE)
            return 1
        handler = args._handler  # type: ignore[attr-defined]
        return handler(_command_argv(argv), pool=pool)
    except (
        queries.UnknownMetric,
        queries.UnknownWorkout,
        queries.UnknownEcg,
        queries.InvalidParameter,
        ValueError,
    ) as exc:
        # Actionable query-layer error text verbatim (no traceback, no SQL).
        emit_progress(f"error: {exc}")
        return 1
    except Exception:
        # Database/network failure: never echo psycopg text (iron law 1).
        emit_progress(f"error: {ERR_QUERY_FAILED}; check database connectivity")
        return 1


def _command_argv(argv: list[str] | None) -> list[str]:
    """The subcommand's own argv slice (everything after the command word).

    ``main``'s top-level parse consumes ``health <command>``; the handler
    re-parses the remainder with its per-command parser (single spec table,
    so parse behavior always matches the help text). ``None`` (embedded /
    default invocation) has no command word to skip.
    """
    if not argv:
        return []
    # argv[1:] is safe even when argv[0] is not the command: argparse of the
    # top-level parser has already validated that argv[0] names a command.
    return argv[1:]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
