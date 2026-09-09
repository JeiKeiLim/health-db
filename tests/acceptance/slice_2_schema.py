"""Acceptance tests — slice-2-schema (migrations/0001_init.sql + partitions).

Proves the schema against a REAL postgres:18 container (harness rule: every
migration has a paired test asserting applied schema state; no mocks):

1. ``apply_all`` applies 0001_init.sql; every spec table exists with its
   spec columns, types, keys, and constraints.
2. ``metric_dim.is_cumulative`` seeds per the CUMULATIVE_UNITS parity rule;
   ``metric_dim.name`` is UNIQUE; identity PKs behave.
3. ``raw_samples``: day_kst generated column stores the correct KST day
   (fixed UTC+9 — 23:59:59 KST vs 00:00:00 KST land on different days);
   monthly partitions attach; inserts outside provisioned months fail;
   latest-wins upsert ON CONFLICT (metric_id, source_id, ts) works on the
   partitioned table; FKs enforced.
4. ``ensure_partitions`` auto-provisions current+next month on demand and is
   idempotent; ``ensure_backfill_range`` covers historical months.
5. workouts / workout_heart_rate / routes: ON DELETE CASCADE, jsonb column,
   PK (workout_id, ts).
6. raw_ingest: sha256 UNIQUE duplicate rejection; auth_log + schema_migrations
   shape.

Deselected without docker via the ``pg`` fixture's explicit skip.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

import pytest


def _shared(name: str):
    """Import a helper from tests/acceptance/conftest.py without a package path."""

    spec = importlib.util.spec_from_file_location(
        "_schema_conftest", Path(__file__).parent / "conftest.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, name)


PG_TEST_USER = _shared("PG_TEST_USER")
_start_container = _shared("_start_container")
_wait_ready = _shared("_wait_ready")

pytestmark = [pytest.mark.slice2, pytest.mark.requires_pg]


@pytest.fixture(scope="module")
def pg():
    """Module-scoped container (one for all 26 tests; per-test data reset).

    Overrides the function-scoped conftest ``pg`` — spinning a fresh
    postgres:18 container per test makes this module take ~30 minutes; one
    container + per-test TRUNCATE/DROP-partitions keeps the same isolation
    at a fraction of the cost. Same explicit skip contract as conftest.
    """
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker CLI not available — requires_pg acceptance test (explicit skip)")
    probe = subprocess.run([docker, "info"], capture_output=True, timeout=30)
    if probe.returncode != 0:
        pytest.skip("docker daemon not running — requires_pg acceptance test (explicit skip)")

    from psycopg_pool import ConnectionPool

    from health_db.db.migrations import apply_all

    handle = _start_container(docker)
    try:
        _wait_ready(
            {
                "host": handle.host,
                "port": handle.port,
                "user": PG_TEST_USER,
                "dbname": "health_test",
                "password": handle.password,
            }
        )
        import psycopg

        with psycopg.connect(
            host=handle.host,
            port=handle.port,
            user=PG_TEST_USER,
            dbname="health_test",
            password=handle.password,
        ) as conn:
            conn.execute(f'CREATE SCHEMA "{handle.schema}"')
            conn.commit()

        handle._pool = ConnectionPool(
            name="health-db-test-schema",
            min_size=1,
            max_size=4,
            open=True,
            kwargs=handle.conn_kwargs,
        )
        with handle.pool.connection() as conn:
            apply_all(conn)
        yield handle
    finally:
        handle.close_pool()
        import subprocess as sp

        sp.run([docker, "rm", "-f", handle.container_id], capture_output=True, timeout=60)


@pytest.fixture(autouse=True)
def _reset_data(pg):
    """Per-test isolation on the shared container: empty tables + no partitions."""
    pg.execute(
        """
        TRUNCATE metric_dim, source_dim, raw_samples, daily_rollups, workouts,
                 workout_heart_rate, routes, raw_ingest, auth_log
        """
    )
    for row in pg.rows(
        """
        SELECT c.relname AS name FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
        WHERE i.inhparent = 'raw_samples'::regclass
        """
    ):
        pg.execute(f'DROP TABLE IF EXISTS "{row["name"]}" CASCADE')


@pytest.fixture()
def migrated(pg):
    """pg container with migrations/0001_init.sql applied (module fixture)."""
    assert pg.scalar("SELECT count(*) FROM schema_migrations WHERE version = 1") == 1
    return pg


def column_map(pg, table: str) -> dict[str, str]:
    rows = pg.rows(
        """
        SELECT column_name, data_type FROM information_schema.columns
        WHERE table_name = %s AND table_schema = %s
        """,
        (table, pg.schema),
    )
    return {r["column_name"]: r["data_type"] for r in rows}


def insert_metric(pg, name: str = "step_count", unit: str = "count") -> int:
    return int(
        pg.scalar(
            "INSERT INTO metric_dim (name, display_name, canonical_unit, is_cumulative)"
            " VALUES (%s, %s, %s, %s) RETURNING metric_id",
            (name, name.replace("_", " ").title(), unit, unit in {"count", "km"}),
        )
    )


def insert_source(pg, name: str = "iPhone") -> int:
    return int(pg.scalar("INSERT INTO source_dim (name) VALUES (%s) RETURNING source_id", (name,)))


# ---------------------------------------------------------------------------
# Migration applied + table inventory
# ---------------------------------------------------------------------------


class TestMigrationApplied:
    def test_version_recorded(self, migrated):
        assert migrated.scalar("SELECT version FROM schema_migrations") == 1

    def test_all_spec_tables_exist(self, migrated):
        expected = {
            "metric_dim",
            "source_dim",
            "raw_samples",
            "daily_rollups",
            "workouts",
            "workout_heart_rate",
            "routes",
            "raw_ingest",
            "auth_log",
            "schema_migrations",
        }
        found = {
            r["table_name"]
            for r in migrated.rows(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = %s AND table_type = 'BASE TABLE'
                """,
                (migrated.schema,),
            )
        }
        assert expected <= found

    def test_reapply_is_noop(self, migrated):
        from health_db.db.migrations import apply_all

        with migrated.pool.connection() as conn:
            assert apply_all(conn) == []


# ---------------------------------------------------------------------------
# metric_dim / source_dim
# ---------------------------------------------------------------------------


class TestMetricDim:
    def test_columns_and_types(self, migrated):
        cols = column_map(migrated, "metric_dim")
        assert cols["metric_id"] == "smallint"
        assert cols["name"] == "text"
        assert cols["display_name"] == "text"
        assert cols["canonical_unit"] == "text"
        assert cols["is_cumulative"] == "boolean"
        assert cols["created_at"] == "timestamp with time zone"

    def test_identity_pk_and_defaults(self, migrated):
        first = migrated.scalar(
            "INSERT INTO metric_dim (name, display_name, canonical_unit, is_cumulative)"
            " VALUES (%s, %s, %s, %s) RETURNING metric_id",
            ("a", "A", "count", True),
        )
        second = migrated.scalar(
            "INSERT INTO metric_dim (name, display_name, canonical_unit, is_cumulative)"
            " VALUES (%s, %s, %s, %s) RETURNING metric_id",
            ("b", "B", "%", False),
        )
        assert second == first + 1
        assert migrated.scalar("SELECT count(*) FROM metric_dim") == 2

    def test_name_unique_rejected(self, migrated):
        insert_metric(migrated, "heart_rate")
        with pytest.raises(Exception, match="duplicate key"):
            insert_metric(migrated, "heart_rate")


# ---------------------------------------------------------------------------
# raw_samples: partitioning, generated day_kst, upsert, FKs
# ---------------------------------------------------------------------------


def ensure_partition_for(pg, ts: datetime) -> str:
    """Create the monthly partition covering ``ts`` (mimics ensure_partitions)."""
    start = ts.astimezone(UTC).date().replace(day=1)
    end = date(start.year + 1, 1, 1) if start.month == 12 else date(start.year, start.month + 1, 1)
    name = f"raw_samples_p{start.year}_{start.month:02d}"
    migrated_table = migrated_partition(pg, name)
    if migrated_table is not None:
        return name
    pg.execute(
        f"CREATE TABLE {name} PARTITION OF raw_samples"
        f" FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
    )
    return name


def migrated_partition(pg, name: str) -> str | None:
    found = pg.scalar("SELECT to_regclass(%s)", (f"{pg.schema}.{name}",))
    return str(found) if found else None


class TestRawSamples:
    def test_is_partitioned_by_ts(self, migrated):
        strategy = migrated.scalar(
            """
            SELECT partstrat FROM pg_partitioned_table pt
            JOIN pg_class c ON c.oid = pt.partrelid
            WHERE c.relname = 'raw_samples'
            """
        )
        assert strategy == "r"  # RANGE

    def test_day_kst_is_stored_generated(self, migrated):
        col = migrated.row(
            """
            SELECT attgenerated FROM pg_attribute
            WHERE attrelid = 'raw_samples'::regclass AND attname = 'day_kst'
            """
        )
        assert col is not None and col["attgenerated"] in ("s", 115)  # 's' = STORED

    def test_pk_includes_partition_key(self, migrated):
        cols = migrated.rows(
            """
            SELECT a.attname FROM pg_index i
            JOIN pg_class c ON c.oid = i.indrelid
            JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)
            WHERE i.indisprimary AND c.relname = 'raw_samples'
            ORDER BY a.attname
            """
        )
        assert [r["attname"] for r in cols] == ["metric_id", "source_id", "ts"]

    def test_day_kst_kst_boundary(self, migrated):
        metric = insert_metric(migrated)
        source = insert_source(migrated)
        ensure_partition_for(migrated, datetime(2026, 9, 8, tzinfo=UTC))
        # 2026-09-08 14:59:59.999Z = 23:59:59.999 KST → day 09-08
        # 2026-09-08 15:00:00Z     = 00:00:00     KST → day 09-09
        with migrated.pool.connection() as conn:
            conn.execute(
                "INSERT INTO raw_samples (metric_id, source_id, ts, qty) VALUES (%s, %s, %s, 1)",
                (metric, source, datetime(2026, 9, 8, 14, 59, 59, 999_000, tzinfo=UTC)),
            )
            conn.execute(
                "INSERT INTO raw_samples (metric_id, source_id, ts, qty) VALUES (%s, %s, %s, 2)",
                (metric, source, datetime(2026, 9, 8, 15, 0, 0, tzinfo=UTC)),
            )
        days = [
            r["day_kst"]
            for r in migrated.rows(
                "SELECT day_kst FROM raw_samples ORDER BY ts",
            )
        ]
        assert days == [date(2026, 9, 8), date(2026, 9, 9)]

    def test_upsert_latest_wins_on_partitioned_table(self, migrated):
        metric = insert_metric(migrated, "heart_rate", "count")
        source = insert_source(migrated)
        ts = datetime(2026, 9, 8, 1, 0, 0, tzinfo=UTC)
        ensure_partition_for(migrated, ts)
        with migrated.pool.connection() as conn:
            for qty in (10.0, 99.0, 55.0):  # three overlapping re-sends
                conn.execute(
                    """
                    INSERT INTO raw_samples (metric_id, source_id, ts, qty)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (metric_id, source_id, ts)
                    DO UPDATE SET qty = EXCLUDED.qty,
                                  ingested_at = EXCLUDED.ingested_at
                    """,
                    (metric, source, ts, qty),
                )
        assert migrated.scalar("SELECT count(*) FROM raw_samples") == 1
        assert migrated.scalar("SELECT qty FROM raw_samples") == 55.0

    def test_insert_without_partition_fails(self, migrated):
        metric = insert_metric(migrated)
        source = insert_source(migrated)
        far_past = datetime(2001, 1, 1, tzinfo=UTC)
        with pytest.raises(Exception):
            with migrated.pool.connection() as conn:
                conn.execute(
                    "INSERT INTO raw_samples (metric_id, source_id, ts, qty)"
                    " VALUES (%s, %s, %s, 1)",
                    (metric, source, far_past),
                )
        assert migrated.scalar("SELECT count(*) FROM raw_samples") == 0

    def test_fk_enforced(self, migrated):
        source = insert_source(migrated)
        ensure_partition_for(migrated, datetime.now(UTC))
        with pytest.raises(Exception, match="violates foreign key"):
            with migrated.pool.connection() as conn:
                conn.execute(
                    "INSERT INTO raw_samples (metric_id, source_id, ts, qty)"
                    " VALUES (9999, %s, now(), 1)",
                    (source,),
                )


# ---------------------------------------------------------------------------
# partitions: ensure_partitions / ensure_backfill_range on the real DB
# ---------------------------------------------------------------------------


class TestEnsurePartitionsReal:
    def test_current_and_next_created_on_demand(self, migrated):
        from health_db.db.partitions import ensure_partitions

        with migrated.pool.connection() as conn:
            ensured = ensure_partitions(conn)
        assert len(ensured) == 2
        for name in ensured:
            assert migrated_partition(migrated, name) is not None

        # partition actually accepts an insert for "this month"
        metric = insert_metric(migrated)
        source = insert_source(migrated)
        now_ts = datetime.now(UTC)
        with migrated.pool.connection() as conn:
            conn.execute(
                "INSERT INTO raw_samples (metric_id, source_id, ts, qty) VALUES (%s, %s, %s, 7)",
                (metric, source, now_ts),
            )
        assert migrated.scalar("SELECT count(*) FROM raw_samples") == 1

    def test_idempotent_second_call_noop(self, migrated):
        from health_db.db.partitions import ensure_partitions

        with migrated.pool.connection() as conn:
            ensure_partitions(conn)
        before = partition_names(migrated)

        with migrated.pool.connection() as conn:
            ensure_partitions(conn)
        after = partition_names(migrated)

        assert before, "ensure_partitions created the current/next month partitions"
        assert after == before, "second call created or dropped no partitions"

        # and a third call is still a no-op: the ensured partitions keep their
        # identity (relfilenode), proving no drop/recreate happened
        with migrated.pool.connection() as conn:
            again = ensure_partitions(conn)
        assert set(again) == set(before)
        with migrated.pool.connection() as conn:
            names = partition_names(migrated)
            nodes_before = partition_relfilenodes(migrated)
            ensure_partitions(conn)
            nodes_after = partition_relfilenodes(migrated)
        assert names == before
        assert nodes_after == nodes_before

    def test_backfill_range_creates_historical_partitions(self, migrated):
        from health_db.db.partitions import ensure_backfill_range

        with migrated.pool.connection() as conn:
            ensured = ensure_backfill_range(
                conn, first_day=date(2024, 3, 15), last_day=date(2024, 4, 2)
            )
        assert ensured == ["raw_samples_p2024_03", "raw_samples_p2024_04"]
        metric = insert_metric(migrated)
        source = insert_source(migrated)
        with migrated.pool.connection() as conn:
            conn.execute(
                "INSERT INTO raw_samples (metric_id, source_id, ts, qty) VALUES (%s, %s, %s, 3)",
                (metric, source, datetime(2024, 3, 20, tzinfo=UTC)),
            )
            conn.execute(
                "INSERT INTO raw_samples (metric_id, source_id, ts, qty) VALUES (%s, %s, %s, 4)",
                (metric, source, datetime(2024, 4, 1, 12, tzinfo=UTC)),
            )
        assert migrated.scalar("SELECT count(*) FROM raw_samples") == 2


def partition_names(pg) -> list[str]:
    """Actual raw_samples partitions from the PG catalogs (pg_inherits)."""
    rows = pg.rows(
        """
        SELECT c.relname AS name
        FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        JOIN pg_class p ON p.oid = i.inhparent
        WHERE p.relname = 'raw_samples'
        ORDER BY c.relname
        """
    )
    return [row["name"] for row in rows]


def partition_relfilenodes(pg) -> dict[str, int]:
    """Partition name → relfilenode (changes if a partition was recreated)."""
    rows = pg.rows(
        """
        SELECT c.relname AS name, c.relfilenode AS node
        FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        JOIN pg_class p ON p.oid = i.inhparent
        WHERE p.relname = 'raw_samples'
        """
    )
    return {row["name"]: row["node"] for row in rows}


# ---------------------------------------------------------------------------
# daily_rollups
# ---------------------------------------------------------------------------


class TestDailyRollups:
    def test_columns_and_pk(self, migrated):
        cols = column_map(migrated, "daily_rollups")
        for name in ("sum_qty", "avg_qty", "min_qty", "max_qty"):
            assert cols[name] == "double precision"
        assert cols["sample_count"] == "bigint"
        assert cols["updated_at"] == "timestamp with time zone"
        assert cols["day_kst"] == "date"

    def test_pk_metric_day(self, migrated):
        constraint = migrated.row(
            """
            SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint
            WHERE conrelid = 'daily_rollups'::regclass AND contype = 'p'
            """
        )
        assert "metric_id" in constraint["def"] and "day_kst" in constraint["def"]

    def test_rollup_recompute_delete_insert_cycle(self, migrated):
        """Parity shape of predecessor ingester.py:237-253 — DELETE+INSERT."""
        metric = insert_metric(migrated, "step_count", "count")
        source = insert_source(migrated)
        ensure_partition_for(migrated, datetime(2026, 9, 8, tzinfo=UTC))
        with migrated.pool.connection() as conn:
            conn.execute(
                "INSERT INTO raw_samples (metric_id, source_id, ts, qty)"
                " VALUES (%s, %s, %s, 100), (%s, %s, %s, 200), (%s, %s, %s, 300)",
                (
                    metric,
                    source,
                    datetime(2026, 9, 8, 1, tzinfo=UTC),
                    metric,
                    source,
                    datetime(2026, 9, 8, 2, tzinfo=UTC),
                    metric,
                    source,
                    datetime(2026, 9, 8, 3, tzinfo=UTC),
                ),
            )
            conn.execute(
                "DELETE FROM daily_rollups WHERE metric_id = %s AND day_kst = %s",
                (metric, date(2026, 9, 8)),
            )
            conn.execute(
                """
                INSERT INTO daily_rollups
                      (metric_id, day_kst, sum_qty, avg_qty, min_qty, max_qty,
                       sample_count, updated_at)
                SELECT metric_id, day_kst, sum(qty), avg(qty), min(qty), max(qty),
                       count(*), now()
                FROM raw_samples WHERE metric_id = %s AND day_kst = %s
                GROUP BY metric_id, day_kst
                """,
                (metric, date(2026, 9, 8)),
            )
        row = migrated.row("SELECT * FROM daily_rollups")
        assert row["sum_qty"] == 600.0
        assert row["avg_qty"] == 200.0
        assert row["min_qty"] == 100.0
        assert row["max_qty"] == 300.0
        assert row["sample_count"] == 3
        assert row["day_kst"] == date(2026, 9, 8)  # 01:00Z = 10:00 KST


# ---------------------------------------------------------------------------
# workouts + HR + routes (CASCADE)
# ---------------------------------------------------------------------------


def insert_workout(pg, workout_id: str = "11111111-2222-3333-4444-555555555555") -> None:
    pg.execute(
        """
        INSERT INTO workouts (id, name, start_ts, end_ts, day_kst, duration_sec, intensity,
                              heart_rate_recovery, ingested_at)
        VALUES (%s, 'Outdoor Run', %s, %s, %s, 1800.0, 4.5, %s, now())
        """,
        (
            workout_id,
            datetime(2026, 9, 8, 0, 30, tzinfo=UTC),
            datetime(2026, 9, 8, 1, 0, tzinfo=UTC),
            date(2026, 9, 8),
            '{"min": 120, "max": 165}',
        ),
    )


class TestWorkouts:
    def test_columns(self, migrated):
        cols = column_map(migrated, "workouts")
        assert cols["id"] == "uuid"
        assert cols["intensity"] == "double precision"  # numeric, not TEXT
        assert cols["heart_rate_recovery"] == "jsonb"
        assert cols["active_energy_kcal"] == "double precision"
        assert cols["is_indoor"] == "boolean"
        assert cols["step_count"] == "bigint"

    def test_hr_and_route_cascade_delete(self, migrated):
        wid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        insert_workout(migrated, wid)
        migrated.execute(
            "INSERT INTO workout_heart_rate (workout_id, ts, heart_rate) VALUES (%s, %s, 120)",
            (wid, datetime(2026, 9, 8, 0, 31, tzinfo=UTC)),
        )
        migrated.execute(
            "INSERT INTO routes (workout_id, ts, latitude, longitude) VALUES (%s, %s, 37.5, 127.0)",
            (wid, datetime(2026, 9, 8, 0, 32, tzinfo=UTC)),
        )
        assert migrated.scalar("SELECT count(*) FROM workout_heart_rate") == 1
        assert migrated.scalar("SELECT count(*) FROM routes") == 1
        migrated.execute("DELETE FROM workouts WHERE id = %s", (wid,))
        assert migrated.scalar("SELECT count(*) FROM workout_heart_rate") == 0
        assert migrated.scalar("SELECT count(*) FROM routes") == 0

    def test_hr_pk_dedupes_same_ts(self, migrated):
        wid = "11111111-2222-3333-4444-555555555555"
        insert_workout(migrated, wid)
        ts = datetime(2026, 9, 8, 0, 31, tzinfo=UTC)
        migrated.execute(
            "INSERT INTO workout_heart_rate (workout_id, ts, heart_rate) VALUES (%s, %s, 120)",
            (wid, ts),
        )
        migrated.execute(
            """
            INSERT INTO workout_heart_rate (workout_id, ts, heart_rate) VALUES (%s, %s, 130)
            ON CONFLICT (workout_id, ts) DO UPDATE SET heart_rate = EXCLUDED.heart_rate
            """,
            (wid, ts),
        )
        assert migrated.scalar("SELECT count(*) FROM workout_heart_rate") == 1
        assert migrated.scalar("SELECT heart_rate FROM workout_heart_rate") == 130


# ---------------------------------------------------------------------------
# raw_ingest / auth_log / schema_migrations shapes
# ---------------------------------------------------------------------------


class TestRawIngestAndAudit:
    def test_sha256_unique_rejects_duplicate(self, migrated):
        received = datetime.now(UTC)
        migrated.execute(
            """
            INSERT INTO raw_ingest (sha256, received_at, data_type, payload, outcome)
            VALUES ('abc123', %s, 'metrics', '{"data": {}}'::jsonb, 'stored')
            """,
            (received,),
        )
        with pytest.raises(Exception, match="duplicate key"):
            migrated.execute(
                """
                INSERT INTO raw_ingest (sha256, received_at, data_type, payload, outcome)
                VALUES ('abc123', %s, 'metrics', '{"data": {}}'::jsonb, 'duplicate')
                """,
                (received,),
            )

    def test_jsonb_payload_roundtrips(self, migrated):
        migrated.execute(
            """
            INSERT INTO raw_ingest (sha256, received_at, data_type, payload, outcome)
            VALUES ('def456', %s, 'workouts', '{"data": {"workouts": [1, 2]}}'::jsonb, 'stored')
            """,
            (datetime.now(UTC),),
        )
        payload = migrated.scalar("SELECT payload FROM raw_ingest WHERE sha256 = 'def456'")
        assert payload == {"data": {"workouts": [1, 2]}}

    def test_identity_pk(self, migrated):
        migrated.execute(
            "INSERT INTO raw_ingest (sha256, received_at, data_type, payload, outcome)"
            " VALUES (%s, now(), %s, %s::jsonb, %s)",
            ("x1", "metrics", "{}", "stored"),
        )
        migrated.execute(
            "INSERT INTO raw_ingest (sha256, received_at, data_type, payload, outcome)"
            " VALUES (%s, now(), %s, %s::jsonb, %s)",
            ("x2", "metrics", "{}", "stored"),
        )
        ids = [r["id"] for r in migrated.rows("SELECT id FROM raw_ingest ORDER BY id")]
        assert ids[1] == ids[0] + 1

    def test_auth_log_columns(self, migrated):
        cols = column_map(migrated, "auth_log")
        assert cols["id"] == "bigint"
        assert cols["ts"] == "timestamp with time zone"
        assert cols["ip"] == "text"
        assert cols["user_agent"] == "text"
        assert cols["reason"] == "text"
