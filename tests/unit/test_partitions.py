"""Unit tests — db.partitions: month math, naming, SQL shape (fakes).

No docker: a recording fake connection captures the executed SQL. The
real-database behavior (partitions actually attach, upserts land) is proven
by the slice-2 acceptance suite against a real postgres:18 container.
"""

from __future__ import annotations

from datetime import date

import pytest

from health_db.db import partitions as parts


class FakeConn:
    """Recording connection: remembers executed SQL and returns fixed rows."""

    def __init__(self, rows: list[tuple] | None = None):
        self.executed: list[tuple[str, tuple]] = []
        self._rows = rows or []

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        return FakeResult(self._rows)


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


# ---------------------------------------------------------------------------
# month_range / partition_name (pure month math)
# ---------------------------------------------------------------------------


class TestMonthMath:
    def test_month_range_regular_month(self):
        assert parts.month_range(date(2026, 9, 8)) == (date(2026, 9, 1), date(2026, 10, 1))

    def test_month_range_first_day_is_normalized(self):
        assert parts.month_range(date(2026, 9, 1)) == (date(2026, 9, 1), date(2026, 10, 1))

    def test_month_range_december_rolls_year(self):
        assert parts.month_range(date(2026, 12, 15)) == (date(2026, 12, 1), date(2027, 1, 1))

    @pytest.mark.parametrize(
        ("day", "expected"),
        [
            (date(2026, 9, 8), "raw_samples_p2026_09"),
            (date(2026, 9, 1), "raw_samples_p2026_09"),
            (date(2026, 1, 1), "raw_samples_p2026_01"),
            (date(2026, 12, 31), "raw_samples_p2026_12"),
        ],
    )
    def test_partition_name_zero_pads_month(self, day, expected):
        assert parts.partition_name(day) == expected


# ---------------------------------------------------------------------------
# ensure_partitions: SQL shape, lock, current+next, injectable today
# ---------------------------------------------------------------------------


class TestEnsurePartitions:
    def test_creates_current_and_next_month(self):
        conn = FakeConn()
        ensured = parts.ensure_partitions(conn, today=date(2026, 9, 8))
        assert ensured == ["raw_samples_p2026_09", "raw_samples_p2026_10"]

    def test_advisory_lock_taken_first(self):
        conn = FakeConn()
        parts.ensure_partitions(conn, today=date(2026, 9, 8))
        first_sql = conn.executed[0][0]
        assert "pg_advisory_xact_lock" in first_sql
        assert str(parts.ADVISORY_LOCK_KEY) in first_sql

    def test_partition_sql_is_if_not_exists_and_range_bounds(self):
        conn = FakeConn()
        parts.ensure_partitions(conn, today=date(2026, 9, 8))
        creates = [sql for sql, _ in conn.executed if "CREATE TABLE" in sql]
        assert len(creates) == 2
        assert all("IF NOT EXISTS" in sql for sql in creates), "idempotent re-run required"
        assert all("PARTITION OF raw_samples" in sql for sql in creates)
        # current month: Sep 1 → Oct 1
        assert "'2026-09-01'" in creates[0] and "'2026-10-01'" in creates[0]
        # next month: Oct 1 → Nov 1
        assert "'2026-10-01'" in creates[1] and "'2026-11-01'" in creates[1]

    def test_today_defaults_to_postgres_now_utc(self):
        conn = FakeConn(rows=[(date(2026, 12, 15),)])
        ensured = parts.ensure_partitions(conn)
        assert ensured == ["raw_samples_p2026_12", "raw_samples_p2027_01"]

    def test_idempotent_shape_same_sql_every_run(self):
        a, b = FakeConn(), FakeConn()
        parts.ensure_partitions(a, today=date(2026, 9, 8))
        parts.ensure_partitions(b, today=date(2026, 9, 8))
        assert a.executed == b.executed, "second call must issue identical no-op SQL"

    def test_year_boundary(self):
        conn = FakeConn()
        ensured = parts.ensure_partitions(conn, today=date(2026, 12, 31))
        assert ensured == ["raw_samples_p2026_12", "raw_samples_p2027_01"]


# ---------------------------------------------------------------------------
# ensure_backfill_range: historical coverage
# ---------------------------------------------------------------------------


class TestEnsureBackfillRange:
    def test_single_month(self):
        conn = FakeConn()
        ensured = parts.ensure_backfill_range(
            conn, first_day=date(2024, 5, 3), last_day=date(2024, 5, 20)
        )
        assert ensured == ["raw_samples_p2024_05"]

    def test_multi_month_chronological(self):
        conn = FakeConn()
        ensured = parts.ensure_backfill_range(
            conn, first_day=date(2024, 11, 5), last_day=date(2025, 2, 10)
        )
        assert ensured == [
            "raw_samples_p2024_11",
            "raw_samples_p2024_12",
            "raw_samples_p2025_01",
            "raw_samples_p2025_02",
        ]

    def test_year_boundary_span(self):
        conn = FakeConn()
        ensured = parts.ensure_backfill_range(
            conn, first_day=date(2023, 12, 1), last_day=date(2024, 1, 31)
        )
        assert ensured == ["raw_samples_p2023_12", "raw_samples_p2024_01"]

    def test_empty_when_range_inverted(self):
        conn = FakeConn()
        ensured = parts.ensure_backfill_range(
            conn, first_day=date(2024, 5, 2), last_day=date(2024, 5, 1)
        )
        assert ensured == []
        assert conn.executed == []

    def test_endpoints_included(self):
        conn = FakeConn()
        ensured = parts.ensure_backfill_range(
            conn, first_day=date(2024, 1, 1), last_day=date(2024, 3, 31)
        )
        assert ensured == ["raw_samples_p2024_01", "raw_samples_p2024_02", "raw_samples_p2024_03"]
