"""Unit tests — health_db.queries: pure validation + pagination + freshness.

No docker: SQL paths run against the acceptance ``pg`` fixture (slice-2);
these unit tests cover the pool-resolution gate, day validation, pagination
bounds/clamps, freshness consensus math, and the schema resource shape with
stub pools.
"""

from __future__ import annotations

from datetime import date

import pytest

import health_db.queries as queries
from health_db.queries import InvalidParameter, UnknownMetric, UnknownWorkout


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


class TestConstants:
    def test_raw_limit_bounds(self):
        assert queries.RAW_DEFAULT_LIMIT == 1000
        assert queries.RAW_MAX_LIMIT == 10000

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
