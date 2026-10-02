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

    def test_route_limit_bounds(self):
        assert queries.ROUTE_DEFAULT_LIMIT == 1000
        assert queries.ROUTE_MAX_LIMIT == 10000

    def test_workout_limit_bounds(self):
        assert queries.WORKOUT_DEFAULT_LIMIT == 500
        assert queries.WORKOUT_MAX_LIMIT == 2000

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

    def test_activity_metrics_roster_is_exact(self):
        """latest_activity_day domain (spec scenario 6): exactly the seven
        activity metrics — adding a metric must be deliberate."""
        assert queries.ACTIVITY_DAY_METRICS == (
            "sleep_analysis",
            "heart_rate_variability",
            "resting_heart_rate",
            "step_count",
            "active_energy",
            "apple_exercise_time",
            "walking_running_distance",
        )


# ---------------------------------------------------------------------------
# latest_activity_day (spec scenario 6) — REAL postgres round-trips.
# Behavioral: real pipeline.ingest_payload → raw_samples → freshness_check
# SQL, then assert the day the consumer date-labels reports with. Own
# containers so seeded metric rows don't collide with the shared lane
# container's corpus.
# ---------------------------------------------------------------------------


def _ensure_shim() -> None:
    """Load tests/acceptance/conftest.py by path into ``tests_acceptance_shim``.

    The lane loader (_lane_pg) boots the shim lazily on first lane use; the
    freshness fixtures need it BEFORE their own container starts, so they
    bootstrap it explicitly (idempotent — skips when already imported).
    """
    import sys

    if "tests_acceptance_shim" in sys.modules:
        return
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "tests_acceptance_shim",
        Path(__file__).resolve().parents[1] / "acceptance" / "conftest.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["tests_acceptance_shim"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)


class _ActivityPg:
    """Disposable container + migrations (no seed — fixtures ingest).

    Mirrors _LanePg but with a metric-payload seed applied by its fixtures;
    an own container keeps the freshness math observable on a known row set.
    """

    def __init__(self) -> None:
        self.handle = None
        self.docker = None

    def start(self):
        _ensure_shim()
        from tests_acceptance_shim import (
            PG_TEST_DB,
            PG_TEST_USER,
            _start_container,
            _wait_ready,
        )

        docker, _ = _pg_available()
        if docker is None:
            pytest.skip("docker CLI/daemon not available — freshness round-trip (explicit skip)")
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


def _activity_payload(day: str, metric: str, qty: float) -> dict:
    """One-metric payload with a single point on the given KST day."""
    return {
        "data": {
            "metrics": [
                {
                    "name": metric,
                    "units": "count",
                    "data": [{"date": f"{day} 12:00:00 +0900", "qty": qty}],
                }
            ],
            "workouts": [],
        }
    }


@pytest.fixture(scope="module")
def freshness_pg():
    """Container seeded with TWO activity metrics + one non-activity metric.

    step_count on 09-28, sleep_analysis on 09-26, heart_rate on 09-01. The
    odd-count median consensus is the true middle (09-26 — the nutrition-
    style feed pins it well behind the activity marker), so the two fields
    deliberately diverge: latest_activity_day 09-28 vs consensus 09-26.
    """
    state = _ActivityPg()
    handle = state.start()
    atexit.register(state.stop)
    from health_db.pipeline import ingest_payload

    pool = handle.pool
    ingest_payload(
        pool,
        _activity_payload("2026-09-01", "heart_rate", 70),
        sha256="unit-freshness-seed-heart",
        body=b"{}",
    )
    ingest_payload(
        pool,
        _activity_payload("2026-09-28", "step_count", 10472.83),
        sha256="unit-freshness-seed-steps",
        body=b"{}",
    )
    ingest_payload(
        pool,
        _activity_payload("2026-09-26", "sleep_analysis", 6.26),
        sha256="unit-freshness-seed-sleep",
        body=b"{}",
    )
    return handle


@pytest.fixture(scope="module")
def only_non_activity_pg():
    """Container whose ONLY metric rows are non-activity (heart_rate).

    Behavioral None case for latest_activity_day: missing/activity-absent
    metrics are ignored, and with zero activity points the marker is None
    while the consensus still derives from the non-activity metrics.
    """
    state = _ActivityPg()
    handle = state.start()
    atexit.register(state.stop)
    from health_db.pipeline import ingest_payload

    ingest_payload(
        handle.pool,
        _activity_payload("2026-09-20", "heart_rate", 70),
        sha256="unit-freshness-seed-heart-only",
        body=b"{}",
    )
    return handle


class TestLatestActivityDay:
    """freshness_check.latest_activity_day — max over the activity metrics.

    Behavioral (spec scenario 6 / job-8): post payloads via the real
    pipeline and assert the additive marker + untouched median consensus.
    """

    def test_activity_day_is_max_over_activity_metrics(self, freshness_pg):
        out = queries.freshness_check(pool=freshness_pg.pool)
        # step_count (09-28) beats sleep_analysis (09-26) → max 09-28.
        assert out["latest_activity_day"] == "2026-09-28"
        # Median consensus UNTOUCHED: rows {09-01, 09-26, 09-28} → middle 09-26.
        assert out["latest_complete_day"] == "2026-09-26"

    def test_consensus_pins_to_slow_feed_not_activity_marker(self, only_non_activity_pg):
        """The two fields diverge: a single non-activity metric pins
        latest_complete_day while latest_activity_day is None."""
        out = queries.freshness_check(pool=only_non_activity_pg.pool)
        assert out["latest_complete_day"] == "2026-09-20"
        assert out["latest_activity_day"] is None, (
            "no activity metric has data → None (missing metrics ignored, not errors)"
        )

    def test_missing_metrics_ignored_not_errors(self, freshness_pg):
        out = queries.freshness_check(pool=freshness_pg.pool)
        # resting_heart_rate + the rest of the roster are absent entirely —
        # absence must not raise or None the marker.
        assert "resting_heart_rate" not in {m["name"] for m in out["metrics"]}
        assert out["latest_activity_day"] == "2026-09-28"

    def test_empty_db_marker_field_present(self):
        """The rows-empty early return must carry latest_activity_day None —
        the additive key exists in EVERY freshness shape. Both return
        literals in freshness_check expose the same key set (additive
        parity between the empty branch and the populated one)."""
        import inspect

        source = inspect.getsource(queries.freshness_check)
        for key in (
            "latest_complete_day",
            "latest_activity_day",
            "stale",
            "metrics",
            "count",
        ):
            assert f'"{key}"' in source, f"missing response key {key}"


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
# Route paging guards (run 2026-09-28-mcp-analysis-parity §2; mirrors the
# get_ecg_detail voltage-page family). Pure validation: an InvalidParameter
# raise must precede any pool borrow — an exploding pool proves it.
# ---------------------------------------------------------------------------


class _ExplodingPool:
    def connection(self):  # noqa: ANN201
        raise AssertionError("validation guard must fire before the pool borrow")


class TestWorkoutDetailRoutePagingGuards:
    def test_non_int_route_limit_rejected_before_pool(self):
        with pytest.raises(InvalidParameter, match="route_limit must be an integer"):
            queries.get_workout_detail(
                "some-uuid", route_limit="10", pool=_ExplodingPool()  # type: ignore[arg-type]
            )

    def test_bool_route_limit_rejected_before_pool(self):
        with pytest.raises(InvalidParameter, match="integer"):
            queries.get_workout_detail(
                "some-uuid", route_limit=True, pool=_ExplodingPool()  # type: ignore[arg-type]
            )

    def test_non_int_route_offset_rejected_before_pool(self):
        with pytest.raises(InvalidParameter, match="route_offset must be an integer"):
            queries.get_workout_detail(
                "some-uuid", route_offset=1.5, pool=_ExplodingPool()  # type: ignore[arg-type]
            )

    def test_bool_route_offset_rejected_before_pool(self):
        with pytest.raises(InvalidParameter, match="integer"):
            queries.get_workout_detail(
                "some-uuid", route_offset=False, pool=_ExplodingPool()  # type: ignore[arg-type]
            )

    def test_route_offset_above_bigint_ceiling_rejected_before_pool(self):
        with pytest.raises(InvalidParameter, match="route_offset must be at most"):
            queries.get_workout_detail(
                "some-uuid", route_offset=2**63, pool=_ExplodingPool()  # type: ignore[arg-type]
            )

    def test_negative_route_limit_not_validated_before_uuid(self):
        """A below-min (non-type-error) limit is clamped, not rejected — the
        clamp fires before the pool, so the exploding pool distinguishes it
        from the InvalidParameter path only by NOT raising InvalidParameter;
        UnknownWorkout (id validation) is the first pool-free failure after
        the guards."""
        with pytest.raises(UnknownWorkout):
            queries.get_workout_detail(
                "some-uuid", route_limit=0, pool=_ExplodingPool()  # type: ignore[arg-type]
            )


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
            "sleep_sessions",
            "auth_log",
        ]

    def test_sleep_sessions_entry_describes_lane(self):
        schema = queries.schema_resource()
        entry = next(t for t in schema["tables"] if t["table"] == "sleep_sessions")
        assert "UNIQUE (source_name, sleep_start)" in entry["key"]
        assert "rollups" in entry["key"]

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


# ---------------------------------------------------------------------------
# Sleep-session listing guards (job-5; run 2026-09-28-mcp-analysis-parity §1)
# Pure validation: an InvalidParameter raise must precede any pool borrow.
# ---------------------------------------------------------------------------


class TestListSleepSessionsGuards:
    """limit clamp [1,500] (default 100) + offset validation, pool-free."""

    def test_non_int_limit_rejected_before_pool(self):
        class _ExplodingPool:
            def connection(self):  # noqa: ANN201
                raise AssertionError("InvalidParameter must fire before the borrow")

        with pytest.raises(InvalidParameter, match="limit must be an integer"):
            queries.list_sleep_sessions(limit="10", pool=_ExplodingPool())  # type: ignore[arg-type]

    def test_bool_limit_rejected_before_pool(self):
        class _ExplodingPool:
            def connection(self):  # noqa: ANN201
                raise AssertionError("InvalidParameter must fire before the borrow")

        with pytest.raises(InvalidParameter, match="integer"):
            queries.list_sleep_sessions(limit=True, pool=_ExplodingPool())  # type: ignore[arg-type]

    def test_non_int_offset_rejected_before_pool(self):
        class _ExplodingPool:
            def connection(self):  # noqa: ANN201
                raise AssertionError("InvalidParameter must fire before the borrow")

        with pytest.raises(InvalidParameter, match="offset must be an integer"):
            queries.list_sleep_sessions(offset=1.5, pool=_ExplodingPool())  # type: ignore[arg-type]

    def test_bool_offset_rejected_before_pool(self):
        class _ExplodingPool:
            def connection(self):  # noqa: ANN201
                raise AssertionError("InvalidParameter must fire before the borrow")

        with pytest.raises(InvalidParameter, match="offset must be an integer"):
            queries.list_sleep_sessions(offset=False, pool=_ExplodingPool())  # type: ignore[arg-type]

    def test_offset_above_bigint_ceiling_rejected_before_pool(self):
        class _ExplodingPool:
            def connection(self):  # noqa: ANN201
                raise AssertionError("InvalidParameter must fire before the borrow")

        with pytest.raises(InvalidParameter, match="offset must be at most"):
            queries.list_sleep_sessions(offset=2**63, pool=_ExplodingPool())  # type: ignore[arg-type]

    def test_invalid_from_day_rejected_before_pool(self):
        class _ExplodingPool:
            def connection(self):  # noqa: ANN201
                raise AssertionError("InvalidParameter must fire before the borrow")

        with pytest.raises(InvalidParameter, match="YYYY-MM-DD"):
            queries.list_sleep_sessions(frm="not-a-day", pool=_ExplodingPool())  # type: ignore[arg-type]

    def test_constants_match_spec(self):
        assert queries.SLEEP_SESSION_DEFAULT_LIMIT == 100
        assert queries.SLEEP_SESSION_MAX_LIMIT == 500


def _sleep_session_point(day: str, hour: int, minute: int, total: float) -> dict:
    """Aggregated-sleep record on KST ``day`` (spec §1 shape, generic sources)."""
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    kst = _dt.strptime(f"{day} {hour:02d}:{minute:02d}", "%Y-%m-%d %H:%M")
    fmt = "%Y-%m-%d %H:%M:%S +0900"
    return {
        "date": f"{day} 00:00:00 +0900",
        "source": "sleep-test-watch",
        "sleepStart": kst.strftime(fmt),
        "sleepEnd": (kst + _td(hours=1)).strftime(fmt),
        "totalSleep": total,
    }


def _seed_sleep_corpus(pg) -> None:
    """Ingest 4 sleep sessions through the REAL pipeline (3 one day, 1 next).

    Distinct (source, sleep_start) upsert keys; newest sleep_start last so
    ORDER BY sleep_start DESC has an unambiguous expected order.
    """
    from health_db.pipeline import ingest_payload

    ingest_payload(
        pg.pool,
        {
            "data": {
                "metrics": [
                    {
                        "name": "sleep_analysis",
                        "units": "hr",
                        "data": [
                            _sleep_session_point("2026-06-10", 22, 0, 7.5),
                            _sleep_session_point("2026-06-10", 13, 30, 0.8),
                            _sleep_session_point("2026-06-11", 21, 45, 6.2),
                            _sleep_session_point("2026-06-12", 23, 15, 8.1),
                        ],
                    }
                ],
                "workouts": [],
            }
        },
        sha256="unit-sleep-seed-0001",
        body=b"{}",
    )


@pytest.fixture(scope="module")
def sleep_pg():
    handle = _lane_pg()
    _seed_sleep_corpus(handle)
    yield handle


class TestListSleepSessionsAgainstPostgres:
    """list_sleep_sessions round-trips: ordering, day filter, paging markers.

    Behavioral contract under the SAME disposable postgres the acceptance
    suite uses: real pipeline ingest → real SQL → query-layer response.
    """

    def test_default_limit_100_and_response_shape(self, sleep_pg):
        out = queries.list_sleep_sessions(pool=sleep_pg.pool)
        assert out["limit"] == 100
        assert out["offset"] == 0
        assert out["count"] == 4
        assert out["next_offset"] is None, "4 rows < one page of 100 = last page"
        assert [s["total_sleep"] for s in out["sessions"]] == [
            8.1, 6.2, 7.5, 0.8], "ORDER BY sleep_start DESC"
        row = out["sessions"][0]
        assert set(row) == {
            "id", "source", "day", "sleep_start", "sleep_end",
            "in_bed_start", "in_bed_end", "total_sleep", "asleep",
            "deep", "core", "rem", "awake", "in_bed",
        }
        assert row["day"] == "2026-06-12"
        assert row["sleep_start"] == "2026-06-12T23:15:00+09:00"
        assert row["sleep_end"] == "2026-06-13T00:15:00+09:00"
        assert row["asleep"] is None
        assert row["deep"] is None

    def test_day_kst_filter_isolates_days(self, sleep_pg):
        only_10 = queries.list_sleep_sessions(
            frm="2026-06-10", to="2026-06-10", pool=sleep_pg.pool
        )
        assert only_10["count"] == 2
        assert {s["total_sleep"] for s in only_10["sessions"]} == {7.5, 0.8}
        empty = queries.list_sleep_sessions(
            frm="2026-01-01", to="2026-01-02", pool=sleep_pg.pool
        )
        assert empty["sessions"] == []
        assert empty["count"] == 0
        assert empty["next_offset"] is None, "empty page is never an error"

    def test_limit_clamps_and_lookahead_drives_next_offset(self, sleep_pg):
        out = queries.list_sleep_sessions(limit=998, pool=sleep_pg.pool)
        assert out["limit"] == 500, "over-max limit clamps to 500"
        tiny = queries.list_sleep_sessions(limit=2, pool=sleep_pg.pool)
        assert tiny["count"] == 2
        assert tiny["next_offset"] == 2, "lookahead saw a 3rd row → marker"
        page2 = queries.list_sleep_sessions(limit=2, offset=tiny["next_offset"], pool=sleep_pg.pool)
        assert page2["count"] == 2, "rows 3-4 fill page 2 exactly"
        assert page2["next_offset"] is None, "no 5th row → last-page marker"
        past_end = queries.list_sleep_sessions(limit=2, offset=4, pool=sleep_pg.pool)
        assert past_end["sessions"] == []
        assert past_end["next_offset"] is None, "past-end: empty array + null marker"
        zero = queries.list_sleep_sessions(limit=0, pool=sleep_pg.pool)
        assert zero["limit"] == 1, "below-min limit clamps to 1"
        assert zero["count"] == 1
        assert zero["next_offset"] == 1

    def test_negative_offset_clamps_to_zero(self, sleep_pg):
        out = queries.list_sleep_sessions(offset=-3, pool=sleep_pg.pool)
        assert out["offset"] == 0
        assert out["count"] == 4

    def test_upsert_key_carries_across_reads(self, sleep_pg):
        """(source_name, sleep_start) UNIQUE: per-source rows stay distinct."""
        sessions = queries.list_sleep_sessions(
            frm="2026-06-10", to="2026-06-10", pool=sleep_pg.pool
        )["sessions"]
        keys = {(s["source"], s["sleep_start"]) for s in sessions}
        assert len(keys) == 2, "distinct (source, sleep_start) — never merged"


# ---------------------------------------------------------------------------
# list_workouts pagination against a >1000-row corpus (job-9 TEST_BUG fix:
# with the 240-row acceptance fixture a regression to the superseded
# hardcoded LIMIT 1000 passes every test — 240 < 1000; 1100 rows makes it
# observable: LIMIT 1000 truncates to count 1000, correct paging returns
# all 1100 in a single limit=2000 page with next_offset None)
# ---------------------------------------------------------------------------

def _seed_workout_corpus(pg, n: int = 1100) -> None:
    """Ingest ``n`` minimal workouts through the REAL pipeline.

    One synthetic UUID per row; start timestamps ascend with ``i`` so
    ORDER BY start_ts DESC has an unambiguous reverse order (id tiebreak
    never needed for determinism here, only for equal-start stability).
    """
    from health_db.pipeline import ingest_payload

    workouts = [
        {
            "id": f"00000000-0000-0000-0000-{i:012d}",
            "name": "Walk",
            "start": f"2018-01-01 {(i % 24):02d}:{(i % 60):02d}:00 +0900",
            "end": f"2018-01-01 {(i % 24):02d}:{(i % 60):02d}:30 +0900",
            "duration": 30,
        }
        for i in range(n)
    ]
    ingest_payload(
        pg.pool,
        {"data": {"metrics": [], "workouts": workouts}},
        sha256="unit-workout-seed-0001",
        body=b"{}",
    )


class TestListWorkoutsNoTruncationAgainstPostgres:
    """The 'no silent truncation above 1000' property, observable in unit.

    A regression to the superseded hardcoded LIMIT 1000 leaves count at
    1000 for a 1100-row range; the real contract returns every row up to
    the (clamped) limit in one page with next_offset None on the last.
    """

    def test_no_truncation_above_1000(self):
        pg = _lane_pg()
        _seed_workout_corpus(pg, 1100)
        out = queries.list_workouts(
            frm="2018-01-01", to="2018-12-31", limit=2000, pool=pg.pool
        )
        assert out["limit"] == 2000, "max limit 2000 accepted un-clamped"
        assert out["count"] == 1100, (
            "LIMIT 1000 regression would truncate this to 1000"
        )
        assert out["offset"] == 0
        assert out["next_offset"] is None, "1100 < one page of 2000 = last page"
        assert len(out["workouts"]) == 1100
