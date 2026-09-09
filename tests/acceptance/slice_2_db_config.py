"""Acceptance tests — slice-2-db-config (DB foundation).

Proves the three acceptance points of the job against a REAL postgres:18
container (no mocks — harness rule):

1. The ``pg`` fixture works: pool connects, ``SELECT 1`` round-trips, the
   throwaway schema is active, dict-row helpers return real query results.
2. ``apply_all`` applies versioned SQL to the real database, records
   ``schema_migrations`` rows, and is idempotent (second run = no-op).
3. ``GET /api/health`` reports ``"db": true`` against the container and
   ``"db": false`` (still HTTP 200) when the database is unreachable.

Deselected without docker via the ``pg`` fixture's explicit skip.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.slice2, pytest.mark.requires_pg]


# ---------------------------------------------------------------------------
# pg fixture: real container, real pool
# ---------------------------------------------------------------------------


class TestPgFixture:
    def test_pool_connects_and_selects(self, pg):
        with pg.pool.connection() as conn:
            cur = conn.execute("SELECT 1 AS one")
            row = cur.fetchone()
        assert row is not None and row[0] == 1

    def test_search_path_pinned_to_throwaway_schema(self, pg):
        with pg.pool.connection() as conn:
            cur = conn.execute("SELECT current_schema()")
            assert cur.fetchone()[0] == pg.schema

    def test_dict_row_helpers(self, pg):
        pg.execute("CREATE TABLE probe (id int, label text)")
        pg.execute("INSERT INTO probe VALUES (1, 'a'), (2, 'b')")
        assert pg.rows("SELECT id, label FROM probe ORDER BY id") == [
            {"id": 1, "label": "a"},
            {"id": 2, "label": "b"},
        ]
        assert pg.row("SELECT label FROM probe WHERE id = 2") == {"label": "b"}
        assert pg.scalar("SELECT count(*) FROM probe") == 2
        assert pg.scalar("SELECT id FROM probe WHERE id = 99") is None


# ---------------------------------------------------------------------------
# apply_all against real postgres: applies + idempotent
# ---------------------------------------------------------------------------


class TestApplyAllReal:
    def test_apply_twice_is_noop_second_time(self, pg, tmp_path):
        from health_db.db.migrations import applied_versions, apply_all

        (tmp_path / "0001_probe.sql").write_text(
            "CREATE TABLE probe_t (id int PRIMARY KEY);\n", encoding="utf-8"
        )
        (tmp_path / "0002_probe.sql").write_text(
            "INSERT INTO probe_t VALUES (1);\n", encoding="utf-8"
        )

        with pg.pool.connection() as conn:
            first = apply_all(conn, tmp_path)
            assert first == [1, 2]

            rows = conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
            assert [r[0] for r in rows] == [1, 2]
            assert conn.execute("SELECT id FROM probe_t").fetchall() == [(1,)]

            second = apply_all(conn, tmp_path)
            assert second == [], "re-run against a migrated DB must apply nothing"
            assert applied_versions(conn) == {1, 2}
            assert conn.execute("SELECT count(*) FROM probe_t").fetchone()[0] == 1

    def test_partial_failure_leaves_no_version_record(self, pg, tmp_path):
        import psycopg

        from health_db.db.migrations import applied_versions, apply_all

        (tmp_path / "0001_bad.sql").write_text(
            "CREATE TABLE bad_t (id int);\nUPDATE missing_t SET x = 1;\n",
            encoding="utf-8",
        )
        with pg.pool.connection() as conn:
            with pytest.raises(psycopg.errors.UndefinedTable):
                apply_all(conn, tmp_path)
            assert 1 not in applied_versions(conn), "failed migration must not be recorded"
            tables = pg.rows(
                "SELECT count(*) AS n FROM information_schema.tables WHERE table_name = 'bad_t'"
            )
            assert tables[0]["n"] == 0, "failed migration must roll back its DDL"


# ---------------------------------------------------------------------------
# GET /api/health: db connectivity key (200 even when DB is down)
# ---------------------------------------------------------------------------


class TestHealthDbKey:
    @pytest.fixture(autouse=True)
    def _isolate_singleton(self):
        """Start every health test from a clean pool state (no singleton, no
        cached probe verdicts leaking in from another test file)."""
        from health_db.db import pool as db_pool

        db_pool.reset_pool()
        db_pool.clear_check_cache()
        yield
        db_pool.reset_pool()
        db_pool.clear_check_cache()

    @pytest.fixture()
    def client(self):
        from fastapi.testclient import TestClient

        import health_db.config as config

        config.ARCHIVE_ROOT = config.Path("data") / "archive"
        from health_db.main import app

        return TestClient(app)

    def test_health_reports_db_false_when_unreachable(self, client, monkeypatch):
        # Explicitly simulate an unconfigured environment (no PSQL_* keys, no
        # materialized pool): the probe must return db=False and never open a
        # connection to ambient defaults like localhost:5432.
        from health_db.db import pool as db_pool

        for key in (
            db_pool.ENV_PSQL_URL,
            db_pool.ENV_PSQL_PORT,
            db_pool.ENV_PSQL_USER,
            db_pool.ENV_PSQL_TABLE,
            db_pool.ENV_PSQL_PASSWORD,
        ):
            monkeypatch.delenv(key, raising=False)

        def forbidden(settings=None):
            raise AssertionError("health probe must not construct a pool here")

        monkeypatch.setattr(db_pool, "get_pool", forbidden)
        res = client.get("/api/health")
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "ok"
        assert body["db"] is False

    def test_health_reports_db_true_when_container_up(self, client, pg, monkeypatch):
        # Simulate a configured deployment (PSQL_* set) and point the pool
        # singleton at the container; probe via the same check_cached TTL
        # path the endpoint uses.
        from health_db.db import pool as db_pool

        db_pool.reset_pool()
        monkeypatch.setenv(db_pool.ENV_PSQL_URL, pg.host)
        monkeypatch.setenv(db_pool.ENV_PSQL_PORT, str(pg.port))
        settings = db_pool.DbSettings(
            host=pg.host,
            port=pg.port,
            user="health_test",
            dbname="health_test",
            password=pg.password,
        )
        monkeypatch.setattr(db_pool, "get_pool", lambda settings=settings: pg.pool)
        db_pool.clear_check_cache()
        try:
            res = client.get("/api/health")
            assert res.status_code == 200
            assert res.json()["db"] is True
        finally:
            db_pool.reset_pool()
