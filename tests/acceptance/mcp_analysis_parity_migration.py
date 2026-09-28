"""Acceptance tests — migration 0003_sleep_sessions (mcp-analysis-parity job-1).

Proves the sleep-session lane schema against a REAL postgres:18 container
(harness rule: every migration has a paired test asserting applied schema
state; no mocks):

1. ``apply_all`` applies 0001 + 0002 + 0003 on a fresh schema; all three
   versions are recorded and a re-run is a no-op (versioned applier contract).
2. The spec §Database Schema table exists with its spec columns/types/
   nullability; UNIQUE (source_name, sleep_start) is the upsert target;
   ``ingested_at`` carries the now() default.
3. day_kst generated column carries the fixed UTC+9 semantics bucketing on
   ``record_date`` (a record_date at 2026-09-28 00:00 KST = 2026-09-27 15:00
   UTC stays day 2026-09-28) — identical bucketing to the existing
   sleep_analysis rows (iron law 4).
4. Anti-scenario 5: no pre-existing table was touched — 0001/0002 tables
   keep their exact column sets after 0003, and 0003 contains no ALTER/DROP.
5. Upsert target works: ON CONFLICT (source_name, sleep_start) DO UPDATE is
   latest-wins (re-ingest idempotency, spec success criterion 2); NULLable
   stage columns store verbatim payload truth (asleep = 0 sentinel stays 0).
6. The day_kst index exists for the list_sleep_sessions from/to filter.

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
        "_mig3_conftest", Path(__file__).parent / "conftest.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, name)


PG_TEST_USER = _shared("PG_TEST_USER")
_start_container = _shared("_start_container")
_wait_ready = _shared("_wait_ready")

pytestmark = [pytest.mark.mcp_analysis_parity, pytest.mark.requires_pg]

MIGRATION_0003 = "0003_sleep_sessions.sql"


@pytest.fixture(scope="module")
def pg():
    """Module-scoped container (one for all tests; per-test data reset).

    Overrides the function-scoped conftest ``pg`` — same isolation-for-cost
    trade as parse_everything_migration.py. Migrations 0001+0002+0003 are
    applied once here; every test starts from zero rows. Same explicit skip
    contract as conftest.
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
            name="health-db-test-mig-0003",
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
    """Apply migrations once per test (idempotent: first call lands 0001-0003)."""
    from health_db.db.migrations import apply_all

    with pg.pool.connection() as conn:
        apply_all(conn)


@pytest.fixture(autouse=True)
def _reset_data(pg):
    """Per-test isolation on the shared container: empty the new lane table."""
    pg.execute("TRUNCATE sleep_sessions")


# ---------------------------------------------------------------------------
# helpers (same shapes as parse_everything_migration.py)
# ---------------------------------------------------------------------------


def _column_map(pg, table: str) -> dict[str, str]:
    rows = pg.rows(
        """
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (pg.schema, table),
    )
    return {r["column_name"]: r["data_type"] for r in rows}


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


def _index_names(pg, table: str) -> set[str]:
    rows = pg.rows(
        """
        SELECT c2.relname AS index_name
        FROM pg_index i
        JOIN pg_class c ON c.oid = i.indrelid
        JOIN pg_class c2 ON c2.oid = i.indexrelid
        WHERE c.relname = %s
        """,
        (table,),
    )
    return {r["index_name"] for r in rows}


def _column_default(pg, table: str, column: str) -> bool:
    row = pg.row(
        """
        SELECT column_default IS NOT NULL AS has_default
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s AND column_name = %s
        """,
        (pg.schema, table, column),
    )
    return bool(row and row["has_default"])


# ---------------------------------------------------------------------------
# applier contract on real postgres
# ---------------------------------------------------------------------------


class TestMigration0003Applied:
    def test_all_versions_recorded(self, pg):
        from health_db.db.migrations import applied_versions

        with pg.pool.connection() as conn:
            assert applied_versions(conn) == {1, 2, 3}, (
                "module fixture applied 0001 → 0002 → 0003"
            )

    def test_apply_all_applies_all_on_fresh_schema(self, pg, tmp_path):
        """Full applier contract on a virgin schema inside the SAME container:
        a fresh search_path sees no version; apply_all lands [1, 2, 3]."""
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
            name="health-db-test-mig-0003-fresh",
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
            "SELECT applied_at FROM schema_migrations WHERE version = %s", (3,)
        )
        assert row is not None, "0003 must be recorded in schema_migrations"


# ---------------------------------------------------------------------------
# spec §Database Schema shape on real postgres
# ---------------------------------------------------------------------------


class TestSleepSessionsShape:
    def test_sleep_sessions_columns(self, pg):
        cols = _column_map(pg, "sleep_sessions")
        expected_types = {
            "id": "bigint",
            "source_name": "text",
            "record_date": "timestamp with time zone",
            "day_kst": "date",
            "sleep_start": "timestamp with time zone",
            "sleep_end": "timestamp with time zone",
            "in_bed_start": "timestamp with time zone",
            "in_bed_end": "timestamp with time zone",
            "total_sleep": "double precision",
            "asleep": "double precision",
            "deep": "double precision",
            "core": "double precision",
            "rem": "double precision",
            "awake": "double precision",
            "in_bed": "double precision",
            "ingested_at": "timestamp with time zone",
        }
        assert cols == expected_types

    def test_unique_source_sleep_start(self, pg):
        uniques = _unique_indexes(pg, "sleep_sessions")
        assert ("source_name", "sleep_start") in uniques, (
            "UNIQUE (source_name, sleep_start) — latest-wins upsert target"
        )

    def test_ingested_at_default(self, pg):
        assert _column_default(pg, "sleep_sessions", "ingested_at"), (
            "ingested_at NOT NULL DEFAULT now() — audit provenance convention"
        )

    def test_day_kst_index_exists(self, pg):
        assert "sleep_sessions_day_kst_idx" in _index_names(pg, "sleep_sessions"), (
            "spec §Database Schema: day-range filter index on day_kst"
        )

    def test_nullability(self, pg):
        """NOT NULL: id, source_name, record_date, sleep_start, ingested_at.
        Everything else NULLable (missing sleep_end / in-bed fields stay NULL)."""
        rows = pg.rows(
            """
            SELECT column_name, is_nullable
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = 'sleep_sessions'
            """,
            (pg.schema,),
        )
        not_null = {r["column_name"] for r in rows if r["is_nullable"] == "NO"}
        assert not_null == {
            "id",
            "source_name",
            "record_date",
            "sleep_start",
            "ingested_at",
        }


class TestDayKstGenerated:
    def test_day_kst_buckets_on_record_date_kst(self, pg):
        """Fixed UTC+9: record_date 2026-09-27 15:00 UTC (= 2026-09-28 00:00 KST)
        → day 2026-09-28, matching the payload `date` verbatim."""
        pg.execute(
            """
            INSERT INTO sleep_sessions (source_name, record_date, sleep_start, total_sleep)
            VALUES ('Apple Watch', '2026-09-27 15:00:00+00', '2026-09-27 14:15:35+00', 6.267)
            """
        )
        day = pg.scalar("SELECT day_kst FROM sleep_sessions")
        assert str(day) == "2026-09-28"

    def test_day_kst_is_generated_not_writable(self, pg):
        """GENERATED ALWAYS: a direct day_kst insert must be rejected."""
        with pytest.raises(Exception, match="generated"):
            pg.execute(
                """
                INSERT INTO sleep_sessions
                    (source_name, record_date, sleep_start, day_kst)
                VALUES ('Apple Watch', '2026-09-27 15:00:00+00',
                        '2026-09-27 14:15:35+00', '1999-01-01')
                """
            )


class TestUpsertTargetWorks:
    def test_sleep_session_upsert_latest_wins(self, pg):
        """ON CONFLICT (source_name, sleep_start) DO UPDATE is the job-3
        pipeline's contract: same (source, sleep_start) → one row, new values."""
        sql = (
            "INSERT INTO sleep_sessions (source_name, record_date, sleep_start,"
            " sleep_end, total_sleep, asleep) VALUES (%s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (source_name, sleep_start) DO UPDATE SET"
            " record_date = EXCLUDED.record_date,"
            " sleep_end = EXCLUDED.sleep_end,"
            " total_sleep = EXCLUDED.total_sleep,"
            " asleep = EXCLUDED.asleep"
        )
        pg.execute(
            sql,
            (
                "Apple Watch",
                "2026-09-28 00:00:00+09",
                "2026-09-27 23:05:35+09",
                "2026-09-28 05:49:54+09",
                6.4,
                0,
            ),
        )
        pg.execute(
            sql,
            (
                "Apple Watch",
                "2026-09-28 00:00:00+09",
                "2026-09-27 23:05:35+09",
                "2026-09-28 06:10:00+09",
                6.5,
                0,
            ),
        )
        assert pg.scalar("SELECT count(*) FROM sleep_sessions") == 1
        assert pg.scalar("SELECT total_sleep FROM sleep_sessions") == pytest.approx(6.5)
        assert pg.scalar(
            "SELECT sleep_end = '2026-09-28 06:10:00+09'::timestamptz FROM sleep_sessions"
        ) is True

    def test_same_start_different_source_is_two_rows(self, pg):
        """Never-merge: two sources with the same sleep_start stay distinct."""
        for source in ("Apple Watch", "AutoSleep"):
            pg.execute(
                """
                INSERT INTO sleep_sessions (source_name, record_date, sleep_start)
                VALUES (%s, '2026-09-17 00:00:00+09', '2026-09-16 17:35:00+09')
                """,
                (source,),
            )
        assert pg.scalar("SELECT count(*) FROM sleep_sessions") == 2

    def test_sentinel_asleep_zero_stored_verbatim(self, pg):
        """Scenario 4: asleep = 0 sentinel stored verbatim, never re-derived."""
        pg.execute(
            """
            INSERT INTO sleep_sessions
                (source_name, record_date, sleep_start, sleep_end,
                 total_sleep, asleep, in_bed, awake, core, deep, rem)
            VALUES ('Apple Watch', '2026-09-28 00:00:00+09',
                    '2026-09-27 23:05:35+09', '2026-09-28 05:49:54+09',
                    6.2614, 0, 0, 0.262,
                    3.7234, 1.043, 1.495)
            """
        )
        row = pg.row(
            "SELECT asleep, in_bed, total_sleep FROM sleep_sessions"
        )
        assert row["asleep"] == 0, "sentinel verbatim"
        assert row["in_bed"] == 0
        assert row["total_sleep"] == pytest.approx(6.2614)

    def test_missing_optional_fields_stay_null(self, pg):
        """Rows lacking sleep_end / in-bed fields store NULL (spec §1)."""
        pg.execute(
            """
            INSERT INTO sleep_sessions (source_name, record_date, sleep_start)
            VALUES ('AutoSleep', '2026-09-17 00:00:00+09', '2026-09-16 17:35:00+09')
            """
        )
        row = pg.row(
            """
            SELECT sleep_end, in_bed_start, in_bed_end, total_sleep,
                   asleep, deep, core, rem, awake, in_bed
            FROM sleep_sessions
            """
        )
        assert all(v is None for v in row.values())


# ---------------------------------------------------------------------------
# anti-scenario 5: additive-only, existing tables untouched
# ---------------------------------------------------------------------------


class TestAdditiveOnly:
    EXPECTED_PRIOR_COLUMNS = {
        "metric_dim": {
            "metric_id",
            "name",
            "display_name",
            "canonical_unit",
            "is_cumulative",
            "created_at",
        },
        "source_dim": {"source_id", "name", "created_at"},
        "ecgs": {
            "id",
            "source_name",
            "start_ts",
            "end_ts",
            "day_kst",
            "classification",
            "sampling_frequency",
            "average_heart_rate",
            "voltage_count",
            "ingested_at",
        },
        "heart_rate_notifications": {
            "id",
            "source_name",
            "start_ts",
            "end_ts",
            "day_kst",
            "kind",
            "threshold",
            "ingested_at",
        },
    }

    def test_prior_tables_keep_their_columns(self, pg):
        for table, expected in self.EXPECTED_PRIOR_COLUMNS.items():
            cols = set(_column_map(pg, table))
            assert cols == expected, f"{table} must be untouched by 0003"

    def test_migration_file_has_no_alter_or_drop(self):
        sql = (
            Path(__file__).resolve().parents[2] / "migrations" / MIGRATION_0003
        ).read_text(encoding="utf-8")
        upper = sql.upper()
        assert "ALTER TABLE" not in upper and "DROP TABLE" not in upper

    def test_migration_uses_fixed_utc9_expression(self):
        import re

        sql = (
            Path(__file__).resolve().parents[2] / "migrations" / MIGRATION_0003
        ).read_text(encoding="utf-8")
        code = re.sub(r"--[^\n]*", "", sql)  # comments may cite the named-zone hazard
        assert "AT TIME ZONE 'UTC') + interval '9 hours'" in code, (
            "iron law 4: fixed UTC+9 expression family from 0001"
        )
        assert "Asia/Seoul" not in code, "never a named zone"
