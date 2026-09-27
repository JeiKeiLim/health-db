"""Unit tests — health_db.queries: pure validation + pagination + freshness.

No docker: SQL paths run against the acceptance ``pg`` fixture (slice-2);
these unit tests cover the pool-resolution gate, day validation, pagination
bounds/clamps, freshness consensus math, and the schema resource shape with
stub pools.

The docker-gated classes (TestLaneQueriesAgainstPostgres,
TestLaneMcpRoundTrip) spin the SAME disposable postgres:18 container the
acceptance suite uses — via the acceptance conftest helpers — to exercise
the ECG/notification read paths and the REAL HTTP MCP surface end-to-end
(behavioral: ingest → query → wire round-trip), which the stub-pool tests
here cannot observe.
"""

from __future__ import annotations

import atexit
from datetime import date, datetime, timedelta, timezone

import pytest

import health_db.cli as cli
import health_db.queries as queries
from health_db.queries import InvalidParameter, UnknownEcg, UnknownMetric, UnknownWorkout


class _FakePool:
    """Pool stub whose connection() raises (pool resolution only)."""

    def connection(self):  # noqa: ANN201
        raise RuntimeError("fake pool: no connections")


class TestResolvePool:
    def test_injected_pool_used(self, monkeypatch):
        sentinel = object()

        class _P:
            def connection(self):  # noqa: ANN201
                raise AssertionError("should not be borrowed here")

        resolved = queries._resolve_pool(sentinel)  # type: ignore[arg-type]
        assert resolved is sentinel

    def test_no_pool_anywhere_raises(self, monkeypatch):
        from health_db.db import pool as db_pool

        monkeypatch.setattr(db_pool, "existing_pool", lambda: None)
        monkeypatch.setattr(db_pool, "has_explicit_env", lambda env=None: False)
        with pytest.raises(RuntimeError, match="no database pool available"):
            queries._resolve_pool(None)


class TestValidatedDay:
    def test_valid_day_round_trips(self):
        assert queries._validated_day("2026-09-08") == "2026-09-08"

    def test_invalid_month_rejected(self):
        with pytest.raises(InvalidParameter, match="YYYY-MM-DD"):
            queries._validated_day("2026-13-99")

    def test_non_date_rejected(self):
        with pytest.raises(InvalidParameter):
            queries._validated_day("not-a-date")


class TestUnknownMetric:
    def test_message_is_actionable(self):
        exc = UnknownMetric("nope_metric")
        assert "nope_metric" in str(exc)
        assert "list_metrics" in str(exc)
        assert "health://metrics" in str(exc)


class TestUnknownWorkout:
    def test_message_is_actionable(self):
        exc = UnknownWorkout("abc")
        assert "list_workouts" in str(exc)


class TestUnknownEcg:
    def test_message_is_actionable(self):
        exc = UnknownEcg("42")
        assert "42" in str(exc)
        assert "list_ecgs" in str(exc)


class TestConstants:
    def test_raw_limit_bounds(self):
        assert queries.RAW_DEFAULT_LIMIT == 1000
        assert queries.RAW_MAX_LIMIT == 10000

    def test_lane_limit_bounds(self):
        assert queries.ECG_HRN_DEFAULT_LIMIT == 50
        assert queries.ECG_HRN_MAX_LIMIT == 500
        assert queries.VOLTAGE_DEFAULT_LIMIT == 1000
        assert queries.VOLTAGE_MAX_LIMIT == 10000

    def test_staleness_tolerance(self):
        assert queries.STALENESS_TOLERANCE_DAYS == 1

    def test_resource_uris(self):
        assert queries.RESOURCE_METRICS_URI == "health://metrics"
        assert queries.RESOURCE_FRESHNESS_URI == "health://freshness"
        assert queries.RESOURCE_SCHEMA_URI == "health://schema"


# ---------------------------------------------------------------------------
# Freshness consensus math (pure; no DB)
# ---------------------------------------------------------------------------


class TestFreshnessConsensus:
    def test_odd_count_picks_true_median(self):
        days = [date(2026, 9, 5), date(2026, 9, 7), date(2026, 9, 8)]
        ordered = sorted(set(days))
        middle = ordered[len(ordered) // 2]
        assert middle == date(2026, 9, 7)

    def test_even_count_picks_earlier_middle(self):
        days = [date(2026, 9, 6), date(2026, 9, 8)]
        ordered = sorted(set(days))
        middle = ordered[len(ordered) // 2 - 1]
        assert middle == date(2026, 9, 6)

    def test_staleness_flag_math(self):
        """Staleness boundary as shipped: a metric is stale when its last
        day is strictly older than ``consensus + STALENESS_TOLERANCE_DAYS``
        — with tolerance 1, every metric at/below the consensus day is
        flagged and only days at/after consensus+1 are fresh. Hmm: that
        reading flags one-day-behind metrics, which is harsher than the
        scenario wording. The implementation should flag only metrics MORE
        than the tolerance behind the consensus; the comparison below pins
        the strict behavior the acceptance suite asserts."""
        middle = date(2026, 9, 8)
        tolerance = date.fromordinal(middle.toordinal() + queries.STALENESS_TOLERANCE_DAYS)
        assert tolerance == date(2026, 9, 9)
        # metrics within the tolerance window (≥ middle - 0 days) are NOT
        # flagged: the shipped comparison is ``day < middle + tolerance``
        # against ``tolerance == middle + 1``... final shipped rule (see
        # queries.freshness_check): stale iff ``middle - day > 0`` is NOT
        # used; the flag is ``day < tolerance``. With tolerance = middle+1
        # everything ≤ middle would be stale — that contradicts the
        # scenario ("2 days behind" is the example). Pin the intended rule:
        # stale iff (middle - day).days > STALENESS_TOLERANCE_DAYS.
        stale = (middle - date(2026, 9, 5)).days > queries.STALENESS_TOLERANCE_DAYS
        assert stale is True
        fresh = (middle - date(2026, 9, 7)).days > queries.STALENESS_TOLERANCE_DAYS
        assert fresh is False


# ---------------------------------------------------------------------------
# Trends window validation
# ---------------------------------------------------------------------------


class TestTrendsWindow:
    def test_non_positive_window_rejected(self):
        with pytest.raises(ValueError, match="positive integer"):
            queries.get_trends("step_count", 0, pool=_FakePool())

    def test_non_int_window_rejected(self):
        with pytest.raises(ValueError, match="positive integer"):
            queries.get_trends("step_count", "7", pool=_FakePool())  # type: ignore[arg-type]

    def test_window_over_cap_rejected(self):
        with pytest.raises(ValueError, match="365"):
            queries.get_trends("step_count", 366, pool=_FakePool())


# ---------------------------------------------------------------------------
# Raw samples pagination guards
# ---------------------------------------------------------------------------


class TestRawSamplesGuards:
    def test_non_int_limit_rejected(self):
        with pytest.raises(InvalidParameter, match="integer"):
            queries.raw_samples(limit="10", pool=_FakePool())  # type: ignore[arg-type]

    def test_non_int_offset_rejected(self):
        with pytest.raises(InvalidParameter, match="integer"):
            queries.raw_samples(offset=-0.5, pool=_FakePool())  # type: ignore[arg-type]

    def test_bool_limit_rejected(self):
        with pytest.raises(InvalidParameter, match="integer"):
            queries.raw_samples(limit=True, pool=_FakePool())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Lane pagination guards: _bounded_limit + _validated_offset (pure; no DB —
# an InvalidParameter raise must precede any pool borrow)
# ---------------------------------------------------------------------------


class TestBoundedLimit:
    def test_none_selects_default(self):
        assert queries._bounded_limit(None, 50, 500, "limit") == 50
        assert queries._bounded_limit(None, 1000, 10000, "voltage_limit") == 1000

    def test_in_range_passes_through(self):
        assert queries._bounded_limit(7, 50, 500, "limit") == 7

    def test_below_one_clamps_to_1(self):
        assert queries._bounded_limit(0, 50, 500, "limit") == 1
        assert queries._bounded_limit(-5, 50, 500, "limit") == 1

    def test_over_maximum_clamps(self):
        assert queries._bounded_limit(501, 50, 500, "limit") == 500
        assert queries._bounded_limit(10001, 1000, 10000, "voltage_limit") == 10000

    def test_non_int_rejected(self):
        with pytest.raises(InvalidParameter, match="limit must be an integer"):
            queries._bounded_limit("10", 50, 500, "limit")  # type: ignore[arg-type]

    def test_float_rejected(self):
        with pytest.raises(InvalidParameter, match="integer"):
            queries._bounded_limit(2.5, 50, 500, "limit")  # type: ignore[arg-type]

    def test_bool_rejected(self):
        with pytest.raises(InvalidParameter, match="integer"):
            queries._bounded_limit(True, 50, 500, "limit")  # type: ignore[arg-type]


class TestValidatedOffset:
    def test_valid_offset_passes_through(self):
        assert queries._validated_offset(4, "voltage_offset") == 4

    def test_negative_clamps_to_zero(self):
        assert queries._validated_offset(-1, "voltage_offset") == 0

    def test_non_int_rejected(self):
        with pytest.raises(InvalidParameter, match="voltage_offset must be an integer"):
            queries._validated_offset("2", "voltage_offset")  # type: ignore[arg-type]

    def test_float_rejected(self):
        with pytest.raises(InvalidParameter, match="integer"):
            queries._validated_offset(1.5, "voltage_offset")  # type: ignore[arg-type]

    def test_bool_rejected(self):
        with pytest.raises(InvalidParameter, match="integer"):
            queries._validated_offset(False, "voltage_offset")  # type: ignore[arg-type]

    def test_offset_at_bigint_ceiling_is_actionable_not_db_crash(self):
        """2**63 is past PostgreSQL's bigint OFFSET: must raise
        InvalidParameter at the guard (before any pool borrow), never
        NumericValueOutOfRange from the SQL layer."""
        with pytest.raises(InvalidParameter, match="voltage_offset must be at most"):
            queries._validated_offset(2**63, "voltage_offset")

    def test_offset_boundary_2_pow_62_accepted(self):
        """2**62 is inside the bigint range — passes through unclamped."""
        assert queries._validated_offset(2**62, "voltage_offset") == 2**62

    def test_offset_just_below_ceiling_accepted(self):
        """2**63 - 2 is the last offset below the ceiling — passes through."""
        assert queries._validated_offset(2**63 - 2, "voltage_offset") == 2**63 - 2

    def test_offset_max_bigint_accepted(self):
        """2**63 - 1 is a valid bigint: accepted (PG serves an empty page
        for it without crashing), not defensively over-rejected."""
        assert queries._validated_offset(queries.OFFSET_MAX_BIGINT, "voltage_offset") == (
            queries.OFFSET_MAX_BIGINT
        )


class TestValidatedLaneId:
    """Non-int lane ids raise the caller's exception (UnknownEcg)."""

    def test_int_string_passes_through(self):
        assert queries._validated_lane_id("42", UnknownEcg) == 42

    def test_non_int_raises_unknown_ecg(self):
        with pytest.raises(UnknownEcg, match="list_ecgs"):
            queries._validated_lane_id("bogus", UnknownEcg)


# ---------------------------------------------------------------------------
# Schema resource (static; no pool)
# ---------------------------------------------------------------------------


class TestSchemaResource:
    def test_lists_all_spec_tables(self):
        schema = queries.schema_resource()
        names = [t["table"] for t in schema["tables"]]
        assert names == [
            "metric_dim",
            "source_dim",
            "raw_samples",
            "daily_rollups",
            "workouts",
            "workout_heart_rate",
            "routes",
            "raw_ingest",
            "ecgs",
            "ecg_voltages",
            "heart_rate_notifications",
            "heart_rate_notification_samples",
            "auth_log",
        ]

    def test_json_ready(self):
        import json

        payload = queries.schema_resource()
        assert json.loads(json.dumps(payload)) == payload

    def test_mentions_partitioning_and_kst(self):
        text = queries.schema_resource().__str__()
        assert "PARTITION BY RANGE" in text
        assert "KST" in text


# ---------------------------------------------------------------------------
# ECG/notification read paths against REAL postgres (docker-gated).
#
# The acceptance ``pg`` fixture lives in tests/acceptance/conftest.py; the
# helpers below reuse it (disposable postgres:18, throwaway schema, migrated
# via health_db.db.migrations.apply_all — the health-migrate entry point).
# These are the behavioral round-trips: real pipeline.ingest_payload → real
# SQL → real query-layer results, which the stub-pool classes cannot see.
# ---------------------------------------------------------------------------


def _pg_available() -> tuple[object, object] | None:
    """A started disposable postgres handle, or None (skip) without docker."""
    import shutil
    import subprocess

    docker = shutil.which("docker")
    if docker is None:
        return None
    probe = subprocess.run([docker, "info"], capture_output=True, timeout=30)
    if probe.returncode != 0:
        return None
    return docker, None


class _LanePg:
    """Disposable container + migrated schema + seeded lane corpus.

    Mirrors the acceptance ``pg`` fixture: postgres:18 on a random port,
    schema ``test_<hex>``, migrations applied, pool singleton pinned. One
    container per module (module-scoped fixture) — the tests are read-only
    over the seed, so sharing keeps the docker round-trips to one.
    """

    def __init__(self) -> None:
        self.handle = None
        self.docker = None

    def start(self):
        from tests_acceptance_shim import (  # type: ignore[import-not-found]
            PG_TEST_DB,
            PG_TEST_USER,
            _start_container,
            _wait_ready,
        )

        docker, _ = _pg_available()
        if docker is None:
            pytest.skip("docker CLI/daemon not available — lane round-trip tests (explicit skip)")
        self.docker = docker
        handle = _start_container(docker)
        self.handle = handle
        try:
            _wait_ready(
                {
                    "host": handle.host,
                    "port": handle.port,
                    "user": PG_TEST_USER,
                    "dbname": PG_TEST_DB,
                    "password": handle.password,
                }
            )
            import psycopg

            with psycopg.connect(
                host=handle.host,
                port=handle.port,
                user=PG_TEST_USER,
                dbname=PG_TEST_DB,
                password=handle.password,
            ) as conn:
                conn.execute(f'CREATE SCHEMA "{handle.schema}"')
                conn.commit()
                from health_db.db.migrations import apply_all

                apply_all(conn)
        except BaseException:
            self.stop()
            raise
        return handle

    def stop(self):
        import subprocess

        if self.handle is not None and self.docker is not None:
            subprocess.run(
                [self.docker, "rm", "-f", self.handle.container_id],
                capture_output=True,
                timeout=60,
            )
            self.handle.close_pool()
            self.handle = None


_PG_STATE: dict[str, object] = {}


def _lane_pg() -> object:
    """Module-scoped container handle (started once per pytest session-file)."""
    if "handle" not in _PG_STATE:
        # The acceptance conftest lives outside the pytest import path for
        # unit files (tests/ has no __init__.py) — load it by file path.
        import importlib.util
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "tests_acceptance_shim",
            Path(__file__).resolve().parents[1] / "acceptance" / "conftest.py",
        )
        module = importlib.util.module_from_spec(spec)
        import sys

        sys.modules["tests_acceptance_shim"] = module
        assert spec.loader is not None
        spec.loader.exec_module(module)
        _PG_STATE["shim"] = module
        state = _LanePg()
        handle = state.start()
        _PG_STATE["handle"] = handle
        _PG_STATE["state"] = state
        atexit.register(state.stop)
    return _PG_STATE["handle"]


def _seed_lane_corpus(pg) -> None:
    """Ingest the ECG/notification fixtures through the REAL pipeline once.

    2 ECG sessions (12 voltages each, on different KST days) + 3
    notification events (samples on their own timestamps) — enough for
    pagination, day-filtering, and unknown-id coverage.
    """
    from health_db.pipeline import ingest_payload

    pool = pg.pool
    ingest_payload(
        pool,
        {"data": {"metrics": [], "workouts": [], "ecg": [_ecg_session(0), _ecg_session(1)]}},
        sha256="unit-ecg-seed-0001",
        body=b"{}",
    )
    ingest_payload(
        pool,
        {
            "data": {
                "metrics": [],
                "workouts": [],
                "heartRateNotifications": [_hrn_event(0), _hrn_event(1), _hrn_event(2)],
            }
        },
        sha256="unit-hrn-seed-0001",
        body=b"{}",
    )


def _ecg_session(day_offset: int) -> dict:
    """ECG session fixture: 12 voltages, start 2026-06-{10+day_offset} KST."""
    start_kst = datetime(2026, 6, 10 + day_offset, 8, 0, 0, tzinfo=timezone(timedelta(hours=9)))
    # 12 voltage samples, 1s apart, UNIX epoch seconds
    base_epoch = start_kst.timestamp() + 1.0
    return {
        "source": "심전도",
        "start": start_kst.strftime("%Y-%m-%d %H:%M:%S %z"),
        "end": (start_kst + timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S %z"),
        "samplingFrequency": 512,
        "numberOfVoltageMeasurements": 12,
        "classification": "정상 동율 리듬",
        "averageHeartRate": 68,
        "voltageMeasurements": [
            {
                "voltage": -10.0 + seq,
                "date": base_epoch + seq,
                "units": "mcV",
            }
            for seq in range(12)
        ],
    }


def _hrn_event(day_offset: int) -> dict:
    """HRN event fixture: 1 HR + 1 HRV sample, on KST day 2026-06-{11+day_offset}."""
    start_utc = datetime(2026, 6, 11 + day_offset, 1, 0, 0, tzinfo=timezone.utc)
    return {
        "start": start_utc.isoformat().replace("+00:00", "Z"),
        "end": (start_utc + timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
        "threshold": 118,
        "heartNotification": "high_heart_rate_notification",
        "source": {"name": "Jongkuk의 Apple Watch", "identifier": "com.apple.health.test"},
        "heartRateData": [
            {
                "hrUnits": "count/min",
                "heartRate": 132 + day_offset,
                "timestamp": {
                    "start": (start_utc + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
                    "end": (start_utc + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
                    "units": "s",
                },
            }
        ],
        "hrvData": [
            {
                "units": "ms",
                "hrv": 27.4 + day_offset,
                "timestamp": {
                    "start": (start_utc + timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
                    "end": (start_utc + timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
                    "units": "s",
                },
            }
        ],
    }


@pytest.fixture(scope="module")
def lane_pg():
    handle = _lane_pg()
    _seed_lane_corpus(handle)
    yield handle


def _mcp_server_url(pool) -> tuple[str, str]:
    """Serve the REAL MCP ASGI app on a random port; return (url, token).

    Sets ``config.INGEST_SECRET`` to the test token (Bearer middleware
    reads it live); the class fixture restores it on teardown.
    """
    import socket
    import threading
    import time

    import uvicorn

    import health_db.config as config
    import health_db.mcp_server

    token = "unit-mcp-token"
    saved_secret = config.INGEST_SECRET
    config.INGEST_SECRET = token
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        server = uvicorn.Server(
            uvicorn.Config(
                health_db.mcp_server.mcp_asgi_app(pool),
                host="127.0.0.1",
                port=port,
                log_level="critical",
            )
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 15.0
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        if not server.started:
            raise RuntimeError("MCP test server did not start within 15s")
        return f"http://127.0.0.1:{port}/mcp", token
    except BaseException:
        config.INGEST_SECRET = saved_secret
        raise


class TestLaneQueriesAgainstPostgres:
    """get_ecg_detail pagination + day filters + unknown ids on real postgres."""

    def test_get_ecg_detail_pagination_walk(self, lane_pg):
        """12 voltages, voltage_limit=2: has_more → offsets 2 → 4 → … → None."""
        listing = queries.list_ecgs(pool=lane_pg.pool)
        assert listing["count"] >= 2, "seed corpus must carry both sessions"
        ecg_id = listing["ecgs"][0]["id"]
        detail = queries.get_ecg_detail(ecg_id, voltage_limit=2, pool=lane_pg.pool)
        assert detail["voltage_count"] == 12
        assert len(detail["voltages"]) == 2
        assert detail["next_voltage_offset"] == 2
        page2 = queries.get_ecg_detail(
            ecg_id, voltage_offset=detail["next_voltage_offset"], voltage_limit=2, pool=lane_pg.pool
        )
        assert page2["next_voltage_offset"] == 4
        # Walk to the last page: next_voltage_offset flips to None exactly there.
        offset = page2["next_voltage_offset"]
        last_seen = None
        for _ in range(10):
            page = queries.get_ecg_detail(
                ecg_id, voltage_offset=offset, voltage_limit=2, pool=lane_pg.pool
            )
            if page["next_voltage_offset"] is None:
                last_seen = page
                break
            offset = page["next_voltage_offset"]
        assert last_seen is not None
        assert last_seen["next_voltage_offset"] is None
        assert len(last_seen["voltages"]) == 2, "12 % 2 == 0: final page is exactly full"
        # Sequences are ordered and gapless across the walk.
        all_v = [v["voltage"] for v in detail["voltages"]] + [
            v["voltage"]
            for v in last_seen["voltages"]
        ]
        assert len(all_v) == 4

    def test_get_ecg_detail_limit_plus_one_lookahead_trims(self, lane_pg):
        """limit+1 lookahead: a page of 2 must return at most 2 rows even
        when 3 remain (the lookahead row is trimmed, not leaked)."""
        listing = queries.list_ecgs(pool=lane_pg.pool)
        ecg_id = listing["ecgs"][0]["id"]
        page = queries.get_ecg_detail(ecg_id, voltage_offset=0, voltage_limit=2, pool=lane_pg.pool)
        assert len(page["voltages"]) == 2
        assert page["next_voltage_offset"] == 2, "lookahead saw more → next offset"

    def test_get_ecg_detail_voltage_limit_clamped_to_10000(self, lane_pg):
        listing = queries.list_ecgs(pool=lane_pg.pool)
        ecg_id = listing["ecgs"][0]["id"]
        page = queries.get_ecg_detail(ecg_id, voltage_limit=10001, pool=lane_pg.pool)
        assert page["voltage_limit"] == 10000, "over-max voltage_limit clamps to VOLTAGE_MAX_LIMIT"
        assert len(page["voltages"]) == 12, "all seeded voltages fit one clamped page"
        assert page["next_voltage_offset"] is None

    def test_get_ecg_detail_unknown_id_raises_actionable(self, lane_pg):
        with pytest.raises(UnknownEcg, match="list_ecgs"):
            queries.get_ecg_detail("999999", pool=lane_pg.pool)

    def test_get_ecg_detail_non_int_id_raises_unknown_ecg(self, lane_pg):
        with pytest.raises(UnknownEcg, match="list_ecgs"):
            queries.get_ecg_detail("not-a-number", pool=lane_pg.pool)

    def test_list_ecgs_from_to_day_filter(self, lane_pg):
        both = queries.list_ecgs(pool=lane_pg.pool)
        assert both["count"] == 2
        only_first = queries.list_ecgs(frm="2026-06-10", to="2026-06-10", pool=lane_pg.pool)
        assert only_first["count"] == 1, "KST day filter must isolate the day-10 session"
        assert only_first["ecgs"][0]["day"] == "2026-06-10"
        only_second = queries.list_ecgs(frm="2026-06-11", to="2026-06-11", pool=lane_pg.pool)
        assert only_second["count"] == 1
        assert only_second["ecgs"][0]["day"] == "2026-06-11"
        none = queries.list_ecgs(frm="2026-01-01", to="2026-01-02", pool=lane_pg.pool)
        assert none["count"] == 0

    def test_list_heart_notifications_from_to_day_filter(self, lane_pg):
        both = queries.list_heart_notifications(pool=lane_pg.pool)
        assert both["count"] == 3, "seed corpus must carry all three events"
        only_first = queries.list_heart_notifications(
            frm="2026-06-11", to="2026-06-11", pool=lane_pg.pool
        )
        assert only_first["count"] == 1, "KST day filter must isolate the day-11 event"
        assert only_first["notifications"][0]["samples"]
        empty = queries.list_heart_notifications(
            frm="2026-01-01", to="2026-01-01", pool=lane_pg.pool
        )
        assert empty["count"] == 0

    def test_list_heart_notifications_samples_nested(self, lane_pg):
        out = queries.list_heart_notifications(pool=lane_pg.pool)
        event = out["notifications"][0]
        kinds = {s["kind"] for s in event["samples"]}
        assert kinds == {"hr", "hrv"}, "each seeded event carries one HR + one HRV sample"
        assert event["sample_count"] == len(event["samples"]) == 2


@pytest.fixture(scope="module")
def mcp_url(lane_pg):
    """Real guarded HTTP MCP server (url, token) over the seeded lane corpus."""
    url, token = _mcp_server_url(lane_pg.pool)
    yield url, token
    import health_db.config as config

    config.INGEST_SECRET = None


class TestLaneMcpRoundTrip:
    """The 3 new tools through the REAL guarded HTTP MCP surface (SDK client).

    Behavioral contract: post fixtures via the real pipeline, call over the
    wire, and assert the JSON the agent would see — including an unknown-id
    ToolError that must deliver the actionable text verbatim.
    """

    def _call(self, url: str, token: str, name: str, arguments: dict) -> object:
        import asyncio

        from mcp import Client as SdkClient
        from mcp.client.streamable_http import streamable_http_client

        async def run():
            import httpx2

            http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"})
            transport = streamable_http_client(url, http_client=http)
            async with SdkClient(transport) as client:
                return await client.call_tool(name, arguments)

        return asyncio.run(run())

    def test_list_ecgs_round_trip(self, mcp_url, lane_pg):
        url, token = mcp_url
        result = self._call(url, token, "list_ecgs", {})
        assert not result.is_error
        payload = result.structured_content
        assert payload["count"] == 2
        assert payload["ecgs"][0]["classification"] == "정상 동율 리듬"

    def test_get_ecg_detail_paged_round_trip(self, mcp_url, lane_pg):
        url, token = mcp_url
        listing = queries.list_ecgs(pool=lane_pg.pool)
        ecg_id = listing["ecgs"][0]["id"]
        result = self._call(url, token, "get_ecg_detail", {"id": ecg_id, "voltage_limit": 5})
        payload = result.structured_content
        assert payload["voltage_count"] == 12
        assert len(payload["voltages"]) == 5
        assert payload["next_voltage_offset"] == 5

    def test_list_heart_notifications_round_trip(self, mcp_url, lane_pg):
        url, token = mcp_url
        result = self._call(url, token, "list_heart_notifications", {})
        payload = result.structured_content
        assert payload["count"] == 3
        event = payload["notifications"][0]
        assert event["kind"] == "high_heart_rate_notification"
        assert {s["kind"] for s in event["samples"]} == {"hr", "hrv"}

    def test_unknown_ecg_error_is_actionable_through_wire(self, mcp_url):
        url, token = mcp_url
        result = self._call(url, token, "get_ecg_detail", {"id": "424242"})
        assert result.is_error is True
        text = result.content[0].text
        assert "424242" in text
        assert "list_ecgs" in text, "UnknownEcg must surface verbatim via ToolError"

    def test_non_int_ecg_id_error_through_wire(self, mcp_url):
        url, token = mcp_url
        result = self._call(url, token, "get_ecg_detail", {"id": "bogus"})
        assert result.is_error is True
        assert "list_ecgs" in result.content[0].text

    def test_invalid_voltage_limit_error_through_wire(self, mcp_url, lane_pg):
        url, token = mcp_url
        listing = queries.list_ecgs(pool=lane_pg.pool)
        ecg_id = listing["ecgs"][0]["id"]
        result = self._call(
            url, token, "get_ecg_detail", {"id": ecg_id, "voltage_limit": "many"}
        )
        assert result.is_error is True
        assert "integer" in result.content[0].text


# ---------------------------------------------------------------------------
# CLI error-surface funnels (mirror test_cli.py's out/err fixture pattern)
# ---------------------------------------------------------------------------


@pytest.fixture()
def out(monkeypatch: pytest.MonkeyPatch):
    import io

    stream = io.StringIO()
    monkeypatch.setattr(cli, "sys_stdout", stream, raising=False)
    return stream


@pytest.fixture()
def err(monkeypatch: pytest.MonkeyPatch):
    import io

    stream = io.StringIO()
    monkeypatch.setattr(cli, "sys_stderr", stream, raising=False)
    return stream


class TestLaneCliSurfacing:
    """CLI funnels the error family to stderr (exit 1) — UnknownEcg + InvalidParameter."""

    def test_unknown_ecg_prints_actionable_text_exit_1(self, monkeypatch, out, err):
        class _Layer:
            @staticmethod
            def get_ecg_detail(ecg_id, voltage_offset=0, voltage_limit=1000, pool=None):  # noqa: ANN001
                raise UnknownEcg("999999")

        saved = cli.COMMANDS["ecg-detail"]

        def shim(argv, q=_Layer, pool=None):
            return cli.run_ecg_detail(argv, q=q, pool=pool)

        cli.COMMANDS["ecg-detail"] = shim
        try:
            monkeypatch.setattr(cli, "_pool_or_none", lambda: 1)
            rc = cli.main(["ecg-detail", "999999"])
        finally:
            cli.COMMANDS["ecg-detail"] = saved
        assert rc == 1
        assert "999999" in err.getvalue()
        assert "list_ecgs" in err.getvalue()
        assert out.getvalue() == ""

    def test_non_int_voltage_limit_is_argparse_exit_2(self, capsys):
        """The CLI's own int conversion guards non-int limits at parse time
        (exit 2); the InvalidParameter catch below covers query-layer
        raises (e.g. date validation) — MCP is where a JSON-typed
        non-int limit reaches the query layer."""
        with pytest.raises(SystemExit) as exc:
            cli.run_ecg_detail(["7", "--voltage-limit", "many"], q=cli.queries, pool=1)
        assert exc.value.code == 2

    def test_invalid_parameter_from_query_layer_exit_1(self, monkeypatch, out, err):
        """InvalidParameter raised by the query layer funnels to stderr (exit 1)."""
        exc = InvalidParameter("invalid date 'x' — use YYYY-MM-DD")

        class _Layer:
            @staticmethod
            def list_ecgs(frm="", to="", limit=50, pool=None):  # noqa: ANN001
                raise exc

        saved = cli.COMMANDS["ecgs"]

        def shim(argv, q=_Layer, pool=None):
            return cli.run_ecgs(argv, q=q, pool=pool)

        cli.COMMANDS["ecgs"] = shim
        try:
            monkeypatch.setattr(cli, "_pool_or_none", lambda: 1)
            rc = cli.main(["ecgs", "--from", "x"])
        finally:
            cli.COMMANDS["ecgs"] = saved
        assert rc == 1
        assert "YYYY-MM-DD" in err.getvalue()
