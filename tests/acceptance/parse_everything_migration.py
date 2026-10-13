"""Acceptance tests — migration 0002_parse_everything (parse-everything job-1).

Proves the ECG + heart-rate-notification lane schema against a REAL
postgres:18 container (harness rule: every migration has a paired test
asserting applied schema state; no mocks):

1. ``apply_all`` applies 0001 + 0002 on a fresh schema; both versions are
   recorded and a re-run is a no-op (versioned applier contract).
2. Every spec §6 table exists with its spec columns/types/nullability;
   UNIQUE (source_name, start_ts) on both parents; PK (ecg_id, seq) and
   (notification_id, ts, kind) on the children; child FKs cascade.
3. day_kst generated columns carry the fixed UTC+9 semantics (a ts at
   2026-09-17 00:30 KST = 2026-09-16 15:30 UTC stays day 2026-09-17).
4. Anti-scenario 6: no pre-existing table was touched — 0001 tables keep
   their exact column sets after 0002, and 0002 contains no ALTER/DROP.
5. Upsert targets work: ON CONFLICT (source_name, start_ts) DO UPDATE is
   latest-wins on both parents; child PKs reject duplicate re-append.

Deselected without docker via the ``pg`` fixture's explicit skip.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest


def _shared(name: str):
    """Import a helper from tests/acceptance/conftest.py without a package path."""

    spec = importlib.util.spec_from_file_location(
        "_mig_conftest", Path(__file__).parent / "conftest.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, name)


PG_TEST_USER = _shared("PG_TEST_USER")
_start_container = _shared("_start_container")
_wait_ready = _shared("_wait_ready")

pytestmark = [pytest.mark.parse_everything, pytest.mark.requires_pg]

MIGRATION_0002 = "0002_parse_everything.sql"


@pytest.fixture(scope="module")
def pg():
    """Module-scoped container (one for all ~20 tests; per-test data reset).

    Overrides the function-scoped conftest ``pg`` — same isolation-for-cost
    trade as slice_2_schema.py (a fresh postgres per test makes this module
    take ~4 minutes). Migrations 0001+0002 are applied once here; every test
    starts from zero rows. Same explicit skip contract as conftest.
    """
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker CLI not available — requires_pg acceptance test (explicit skip)")
    probe = subprocess.run([docker, "info"], capture_output=True, timeout=30)
    if probe.returncode != 0:
        pytest.skip("docker daemon not running — requires_pg acceptance test (explicit skip)")

    from psycopg_pool import ConnectionPool

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
            name="health-db-test-mig-0002",
            min_size=1,
            max_size=4,
            open=True,
            kwargs=handle.conn_kwargs,
        )
        yield handle
    finally:
        handle.close_pool()
        subprocess.run([docker, "rm", "-f", handle.container_id], capture_output=True, timeout=60)


@pytest.fixture(autouse=True)
def _apply_migrations(pg):
    """Apply migrations once per test (idempotent: first call lands 0001+0002)."""
    from health_db.db.migrations import apply_all

    with pg.pool.connection() as conn:
        apply_all(conn)


@pytest.fixture(autouse=True)
def _reset_data(pg):
    """Per-test isolation on the shared container: empty the new lane tables."""
    pg.execute(
        "TRUNCATE ecg_voltages, ecgs, heart_rate_notification_samples, heart_rate_notifications"
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _column_map(pg, table: str) -> dict[str, str]:
    rows = pg.rows(
        """
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (pg.schema, table),
    )
    return {r["column_name"]: r["data_type"] for r in rows}


def _pk_columns(pg, table: str) -> list[str]:
    rows = pg.rows(
        """
        SELECT a.attname FROM pg_index i
        JOIN pg_class c ON c.oid = i.indrelid
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)
        WHERE i.indisprimary AND c.relname = %s
        ORDER BY a.attname
        """,
        (table,),
    )
    return [r["attname"] for r in rows]


def _unique_indexes(pg, table: str) -> set[tuple[str, ...]]:
    rows = pg.rows(
        """
        SELECT i.indisprimary, a.attname, i.indkey::text
        FROM pg_index i
        JOIN pg_class c ON c.oid = i.indrelid
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)
        WHERE i.indisunique AND c.relname = %s
        ORDER BY i.indkey::text, a.attnum
        """,
        (table,),
    )
    out: set[tuple[str, ...]] = set()
    current: tuple[str, ...] = tuple()
    key_text: str | None = None
    for r in rows:
        if key_text is not None and r["indkey"] != key_text:
            out.add(current)
            current = tuple()
        key_text = r["indkey"]
        current += (r["attname"],)
    if key_text is not None:
        out.add(current)
    return out


def _cascade_fk(pg, child: str, parent: str) -> bool:
    row = pg.row(
        """
        SELECT confdeltype = 'c' AS cascade
        FROM pg_constraint
        WHERE contype = 'f' AND conrelid = %s::regclass
          AND confrelid = %s::regclass
        """,
        (f"{pg.schema}.{child}", f"{pg.schema}.{parent}"),
    )
    return bool(row and row["cascade"])


# ---------------------------------------------------------------------------
# applier contract on real postgres
# ---------------------------------------------------------------------------


class TestMigration0002Applied:
    def test_both_versions_recorded(self, pg):
        from health_db.db.migrations import applied_versions

        with pg.pool.connection() as conn:
            assert applied_versions(conn) == {1, 2, 3}, (
                "module fixture applied 0001 → 0002 → 0003"
            )

    def test_apply_all_applies_both_on_fresh_schema(self, pg, tmp_path):
        """Full applier contract on a virgin schema inside the SAME container:
        a fresh search_path sees neither version; apply_all lands [1, 2, 3]."""
        import psycopg
        from psycopg_pool import ConnectionPool

        from health_db.db.migrations import applied_versions, apply_all

        fresh = f"{pg.schema}_fresh"
        with psycopg.connect(
            host=pg.host, port=pg.port, user=PG_TEST_USER,
            dbname="health_test", password=pg.password,
        ) as conn:
            conn.execute(f'CREATE SCHEMA "{fresh}"')
            conn.commit()
        probe = ConnectionPool(  # noqa: F841 (closed via context below)
            name="health-db-test-mig-fresh",
            min_size=1,
            max_size=1,
            open=True,
            kwargs={**pg.conn_kwargs, "options": f"-c search_path={fresh},public"},
        )
        try:
            with probe.connection() as conn:
                applied = apply_all(conn)
                assert applied == [1, 2, 3], "fresh schema: 0001 → 0002 → 0003, in order"
                assert applied_versions(conn) == {1, 2, 3}
                assert apply_all(conn) == [], "re-run against a migrated DB applies nothing"
        finally:
            probe.close()
            with psycopg.connect(
                host=pg.host, port=pg.port, user=PG_TEST_USER,
                dbname="health_test", password=pg.password,
            ) as conn:
                conn.execute(f'DROP SCHEMA "{fresh}" CASCADE')
                conn.commit()

    def test_versions_table_shape(self, pg):
        row = pg.row(
            "SELECT applied_at FROM schema_migrations WHERE version = %s", (2,)
        )
        assert row is not None, "0002 must be recorded in schema_migrations"


# ---------------------------------------------------------------------------
# spec §6 schema shape on real postgres
# ---------------------------------------------------------------------------


class TestEcgTablesShape:
    def test_ecgs_columns(self, pg):
        cols = _column_map(pg, "ecgs")
        expected_types = {
            "id": "bigint",
            "source_name": "text",
            "start_ts": "timestamp with time zone",
            "end_ts": "timestamp with time zone",
            "day_kst": "date",
            "classification": "text",
            "sampling_frequency": "integer",
            "average_heart_rate": "integer",
            "voltage_count": "integer",
            "ingested_at": "timestamp with time zone",
        }
        assert cols == expected_types

    def test_ecg_voltages_columns(self, pg):
        cols = _column_map(pg, "ecg_voltages")
        expected_types = {
            "ecg_id": "bigint",
            "seq": "integer",
            "ts": "timestamp with time zone",
            "voltage": "double precision",
            "units": "text",
        }
        assert cols == expected_types

    def test_ecgs_unique_source_start(self, pg):
        uniques = _unique_indexes(pg, "ecgs")
        assert ("source_name", "start_ts") in uniques

    def test_ecg_voltages_pk(self, pg):
        assert _pk_columns(pg, "ecg_voltages") == ["ecg_id", "seq"]

    def test_ecg_voltages_cascade_fk(self, pg):
        assert _cascade_fk(pg, "ecg_voltages", "ecgs")


class TestNotificationTablesShape:
    def test_heart_rate_notifications_columns(self, pg):
        cols = _column_map(pg, "heart_rate_notifications")
        expected_types = {
            "id": "bigint",
            "source_name": "text",
            "start_ts": "timestamp with time zone",
            "end_ts": "timestamp with time zone",
            "day_kst": "date",
            "kind": "text",
            "threshold": "integer",
            "ingested_at": "timestamp with time zone",
        }
        assert cols == expected_types

    def test_heart_rate_notification_samples_columns(self, pg):
        cols = _column_map(pg, "heart_rate_notification_samples")
        expected_types = {
            "notification_id": "bigint",
            "ts": "timestamp with time zone",
            "kind": "text",
            "value": "double precision",
            "units": "text",
        }
        assert cols == expected_types

    def test_notifications_unique_source_start(self, pg):
        uniques = _unique_indexes(pg, "heart_rate_notifications")
        assert ("source_name", "start_ts") in uniques

    def test_samples_pk_includes_kind(self, pg):
        assert _pk_columns(pg, "heart_rate_notification_samples") == [
            "kind",
            "notification_id",
            "ts",
        ], "PK (notification_id, ts, kind) — set equality regardless of column order"

    def test_samples_cascade_fk(self, pg):
        assert _cascade_fk(pg, "heart_rate_notification_samples", "heart_rate_notifications")


class TestDayKstGenerated:
    def test_ecgs_day_kst_is_kst_day(self, pg):
        """Fixed UTC+9: ts 2026-09-16 15:30 UTC (= 00:30 KST) → day 2026-09-17."""
        pg.execute(
            """
            INSERT INTO ecgs (source_name, start_ts, end_ts, voltage_count)
            VALUES ('심전도', '2026-09-16 15:30:00+00', '2026-09-16 15:31:00+00', 1)
            """
        )
        day = pg.scalar("SELECT day_kst FROM ecgs")
        assert str(day) == "2026-09-17"

    def test_notifications_day_kst_is_kst_day(self, pg):
        pg.execute(
            """
            INSERT INTO heart_rate_notifications (source_name, start_ts, end_ts, kind)
            VALUES ('Apple Watch', '2026-09-16 15:30:00+00', '2026-09-16 15:40:00+00', 'high')
            """
        )
        day = pg.scalar("SELECT day_kst FROM heart_rate_notifications")
        assert str(day) == "2026-09-17"


# ---------------------------------------------------------------------------
# anti-scenario 6: additive-only, existing tables untouched
# ---------------------------------------------------------------------------


class TestAdditiveOnly:
    EXPECTED_0001_COLUMNS = {
        "metric_dim": {
            "metric_id", "name", "display_name", "canonical_unit", "is_cumulative", "created_at",
        },
        "source_dim": {"source_id", "name", "created_at"},
        "workouts": {
            "id", "name", "start_ts", "end_ts", "day_kst", "duration_sec",
            "active_energy_kcal", "total_energy", "basal_energy", "mets",
            "temperature", "humidity", "is_indoor", "location", "intensity",
            "distance_km", "walking_running_distance_km", "step_count",
            "step_cadence", "speed", "avg_heart_rate", "max_heart_rate",
            "heart_rate_recovery", "ingested_at",
        },
        "raw_ingest": {
            "id", "sha256", "received_at", "automation_name", "automation_id",
            "session_id", "data_type", "payload", "outcome", "error_note",
        },
    }

    def test_0001_tables_keep_their_columns(self, pg):
        for table, expected in self.EXPECTED_0001_COLUMNS.items():
            cols = set(_column_map(pg, table))
            assert cols == expected, f"{table} must be untouched by later migrations"

    def test_migration_file_has_no_alter_or_drop(self):
        from pathlib import Path

        sql = (
            Path(__file__).resolve().parents[2] / "migrations" / MIGRATION_0002
        ).read_text(encoding="utf-8")
        upper = sql.upper()
        assert "ALTER TABLE" not in upper and "DROP TABLE" not in upper


# ---------------------------------------------------------------------------
# upsert targets behave (latest-wins keys usable by the pipeline, job-7)
# ---------------------------------------------------------------------------


class TestUpsertTargetsWork:
    def test_ecg_upsert_latest_wins(self, pg):
        pg.execute(
            """
            INSERT INTO ecgs (source_name, start_ts, end_ts, voltage_count, average_heart_rate)
            VALUES ('심전도', '2025-10-12 01:45:06+00', '2025-10-12 01:45:36+00', 3, 68)
            ON CONFLICT (source_name, start_ts) DO UPDATE
            SET voltage_count = EXCLUDED.voltage_count,
                average_heart_rate = EXCLUDED.average_heart_rate
            """
        )
        pg.execute(
            """
            INSERT INTO ecgs (source_name, start_ts, end_ts, voltage_count, average_heart_rate)
            VALUES ('심전도', '2025-10-12 01:45:06+00', '2025-10-12 01:45:36+00', 4, 69)
            ON CONFLICT (source_name, start_ts) DO UPDATE
            SET voltage_count = EXCLUDED.voltage_count,
                average_heart_rate = EXCLUDED.average_heart_rate
            """
        )
        assert pg.scalar("SELECT count(*) FROM ecgs") == 1
        assert pg.scalar("SELECT voltage_count FROM ecgs") == 4

    def test_ecg_voltage_pk_rejects_reappend(self, pg):
        pg.execute(
            """
            INSERT INTO ecgs (source_name, start_ts, end_ts, voltage_count)
            VALUES ('심전도', '2025-10-12 01:45:06+00', '2025-10-12 01:45:36+00', 1)
            """
        )
        ecg_id = pg.scalar("SELECT id FROM ecgs")
        sql = (
            "INSERT INTO ecg_voltages (ecg_id, seq, ts, voltage, units)"
            " VALUES (%s, 0, %s, %s, 'mcV')"
        )
        pg.execute(sql, (ecg_id, "2025-10-12 01:45:06.862702+00", -10.53))
        import pytest as _pytest

        with _pytest.raises(Exception, match="duplicate key"):
            pg.execute(sql, (ecg_id, "2025-10-12 01:45:06.864655+00", -8.96))

    def test_notification_sample_pk_accepts_same_ts_both_kinds(self, pg):
        """A single ts may carry BOTH an HR and an HRV sample (kind in the PK)."""
        pg.execute(
            """
            INSERT INTO heart_rate_notifications (source_name, start_ts, end_ts, kind)
            VALUES ('Apple Watch', '2026-06-16 04:39:20+00', '2026-06-16 04:39:20+00',
                    'high_heart_rate_notification')
            """
        )
        nid = pg.scalar("SELECT id FROM heart_rate_notifications")
        ts = "2026-06-16 04:40:29+00"
        sql = (
            "INSERT INTO heart_rate_notification_samples"
            " (notification_id, ts, kind, value, units) VALUES (%s, %s, %s, %s, %s)"
        )
        pg.execute(sql, (nid, ts, "hr", 132.0, "count/min"))
        pg.execute(sql, (nid, ts, "hrv", 27.41, "ms"))
        assert pg.scalar("SELECT count(*) FROM heart_rate_notification_samples") == 2

    def test_child_rows_cascade_on_parent_delete(self, pg):
        pg.execute(
            """
            INSERT INTO ecgs (source_name, start_ts, end_ts, voltage_count)
            VALUES ('심전도', '2025-10-12 01:45:06+00', '2025-10-12 01:45:36+00', 1)
            """
        )
        pg.execute(
            """
            INSERT INTO ecg_voltages (ecg_id, seq, ts, voltage, units)
            SELECT id, 0, '2025-10-12 01:45:06.862702+00', -10.53, 'mcV' FROM ecgs
            """
        )
        pg.execute("DELETE FROM ecgs")
        assert pg.scalar("SELECT count(*) FROM ecg_voltages") == 0, "FK ON DELETE CASCADE"
