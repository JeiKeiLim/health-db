"""Unit tests — db.migrations: generic versioned-SQL applier with fakes.

No docker: a recording fake connection executes migration SQL against a
light in-memory model (CREATE TABLE adds a table name; INSERT records the
version). The real-SQL behavior (actual schema_migrations table on
postgres) is covered by the slice-2 acceptance ``pg`` fixture.
"""

from __future__ import annotations

import re

import pytest

from health_db.db import migrations as mig


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class FakeTransaction:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.conn.rollback_count += 1
        return False


class FakeConn:
    """Minimal recording connection over an in-memory migration model."""

    def __init__(self):
        self.executed: list[tuple[str, tuple]] = []
        self.versions: list[int] = []
        self.tables: set[str] = set()
        self.rollback_count = 0

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        sql_l = " ".join(sql.split()).lower()
        if sql_l.startswith("create table"):
            self.tables.add(sql_l.split("(", 1)[0].split()[-1])
            return FakeResult([])
        if sql_l.startswith("insert into schema_migrations"):
            self.versions.append(int(params[0]))
            return FakeResult([])
        if sql_l.startswith("select version from schema_migrations"):
            return FakeResult([(v,) for v in self.versions])
        return FakeResult([])

    def transaction(self):
        return FakeTransaction(self)


def write_migration(root, version_name: str, content: str = "SELECT 1;\n"):
    root.joinpath(version_name).write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# discover: file naming, ordering, duplicates
# ---------------------------------------------------------------------------


class TestDiscover:
    def test_ordered_by_version(self, tmp_path):
        write_migration(tmp_path, "0002_two.sql")
        write_migration(tmp_path, "0001_one.sql")
        write_migration(tmp_path, "0010_ten.sql")
        found = mig.discover(tmp_path)
        assert [m.version for m in found] == [1, 2, 10]

    def test_ignores_non_sql_and_unversioned_files(self, tmp_path):
        write_migration(tmp_path, "0001_one.sql")
        write_migration(tmp_path, "readme.txt")
        write_migration(tmp_path, "notes.md")
        (tmp_path / "0001_dup").mkdir()
        assert [m.version for m in mig.discover(tmp_path)] == [1]

    def test_duplicate_version_raises(self, tmp_path):
        write_migration(tmp_path, "0001_a.sql")
        write_migration(tmp_path, "0001_b.sql")
        with pytest.raises(mig.MigrationError, match="duplicate"):
            mig.discover(tmp_path)

    def test_missing_directory_raises(self, tmp_path):
        with pytest.raises(mig.MigrationError, match="not found"):
            mig.discover(tmp_path / "nope")


# ---------------------------------------------------------------------------
# apply_all: idempotency + bookkeeping table
# ---------------------------------------------------------------------------


class TestApplyAll:
    def test_applies_pending_in_order_and_records(self, tmp_path):
        write_migration(tmp_path, "0001_first.sql", "SELECT 'a';")
        write_migration(tmp_path, "0002_second.sql", "SELECT 'b';")
        conn = FakeConn()
        applied = mig.apply_all(conn, tmp_path)
        assert applied == [1, 2]
        assert conn.versions == [1, 2]
        assert any(
            "CREATE TABLE IF NOT EXISTS schema_migrations" in sql for sql, _ in conn.executed
        )

    def test_idempotent_second_run_applies_nothing(self, tmp_path):
        write_migration(tmp_path, "0001_first.sql", "SELECT 'a';")
        conn = FakeConn()
        assert mig.apply_all(conn, tmp_path) == [1]
        assert mig.apply_all(conn, tmp_path) == []
        assert conn.versions == [1], "re-run must not re-apply or re-record"

    def test_skips_only_already_applied(self, tmp_path):
        write_migration(tmp_path, "0001_first.sql")
        write_migration(tmp_path, "0002_second.sql")
        conn = FakeConn()
        conn.versions = [1]
        assert mig.apply_all(conn, tmp_path) == [2]
        assert conn.versions == [1, 2]

    def test_partial_migration_fails_and_is_not_recorded(self, tmp_path):
        class ExplodingConn(FakeConn):
            def execute(self, sql, params=()):
                bookkeeping = "schema_migrations" in sql or "INSERT" in sql
                if "SELECT 1" not in sql and not bookkeeping:
                    raise RuntimeError("boom")
                return super().execute(sql, params)

        write_migration(tmp_path, "0001_bad.sql", "UPDATE nope;")
        conn = ExplodingConn()
        with pytest.raises(RuntimeError):
            mig.apply_all(conn, tmp_path)
        assert 1 not in conn.versions, "failed migration must not be marked applied"

    def test_no_migrations_dir_is_noop(self, tmp_path):
        conn = FakeConn()
        assert mig.apply_all(conn, tmp_path / "missing") == []
        assert "CREATE TABLE IF NOT EXISTS schema_migrations" in conn.executed[0][0]

    def test_read_real_sql_content_verbatim(self, tmp_path):
        sql = "CREATE TABLE t (id int);\n"
        write_migration(tmp_path, "0001_t.sql", sql)
        conn = FakeConn()
        mig.apply_all(conn, tmp_path)
        assert (sql, ()) in conn.executed


# ---------------------------------------------------------------------------
# migration 0002 file contract (parse-everything job-1; real-SQL behavior on
# postgres is covered by tests/acceptance/parse_everything_migration.py)
# ---------------------------------------------------------------------------


class TestMigration0002File:
    """The committed 0002 file must be discoverable and additive-only.

    Structure-level contract, docker-free: every non-comment statement is a
    CREATE TABLE for exactly the four spec §6 tables, with no ALTER/DROP
    anywhere (anti-scenario 6: 0001 is frozen). Runtime application +
    idempotency against a disposable postgres live in the requires_pg
    acceptance suite (harness rule: every migration has a paired test
    asserting applied schema state, no mocks).
    """

    REPO_MIGRATIONS = mig.Path(__file__).resolve().parents[2] / "migrations"
    EXPECTED_TABLES = {
        "ecgs",
        "ecg_voltages",
        "heart_rate_notifications",
        "heart_rate_notification_samples",
    }

    def _statements(self) -> list[str]:
        import re

        sql = (self.REPO_MIGRATIONS / "0002_parse_everything.sql").read_text(encoding="utf-8")
        sql = re.sub(r"--[^\n]*", "", sql)  # strip line + inline comments
        return [stmt.strip() for stmt in sql.split(";") if stmt.strip()]

    def test_discovered_as_version_2_after_0001(self, monkeypatch):
        monkeypatch.delenv(mig.ENV_MIGRATIONS_DIR, raising=False)
        found = mig.discover(mig.migrations_dir())
        assert [m.version for m in found] == [1, 2, 3]
        assert found[1].path.name == "0002_parse_everything.sql"
        assert found[2].path.name == "0003_sleep_sessions.sql"

    def test_every_statement_is_create_table_for_the_four_spec_tables(self):
        statements = self._statements()
        assert statements, "0002 must contain statements"
        created: set[str] = set()
        for stmt in statements:
            upper = " ".join(stmt.upper().split())
            assert upper.startswith("CREATE TABLE"), f"non-additive statement: {stmt[:60]!r}"
            created.add(stmt.split("(", 1)[0].split()[-1].lower())
        assert created == self.EXPECTED_TABLES

    def test_no_alter_or_drop_anywhere(self):
        import re

        sql = (self.REPO_MIGRATIONS / "0002_parse_everything.sql").read_text(encoding="utf-8")
        code = re.sub(r"--[^\n]*", "", sql).upper()
        assert "ALTER TABLE" not in code and "DROP TABLE" not in code

    def test_applies_verbatim_through_fake_conn(self):
        """The real file flows through apply_all unmodified (generic applier)."""
        conn = FakeConn()
        applied = mig.apply_all(conn, self.REPO_MIGRATIONS)
        assert applied == [1, 2, 3], "fresh fake DB: 0001 → 0002 → 0003, in order"
        assert conn.versions == [1, 2, 3]
        file_sql = (self.REPO_MIGRATIONS / "0002_parse_everything.sql").read_text(encoding="utf-8")
        assert any(sql == file_sql for sql, _ in conn.executed), (
            "0002 must be executed verbatim — the applier never inspects or rewrites migration SQL"
        )


# ---------------------------------------------------------------------------
# migration 0003 file contract (mcp-analysis-parity job-1; real-SQL behavior on
# postgres is covered by tests/acceptance/mcp_analysis_parity_migration.py)
# ---------------------------------------------------------------------------


class TestMigration0003File:
    """The committed 0003 file must be discoverable and additive-only.

    Structure-level contract, docker-free: every non-comment statement is a
    CREATE TABLE (sleep_sessions) or CREATE INDEX (day_kst filter support),
    with no ALTER/DROP anywhere (anti-scenario 5: 0001/0002 are frozen).
    Runtime application + column shape + day_kst semantics against a
    disposable postgres live in the requires_pg acceptance suite.
    """

    REPO_MIGRATIONS = mig.Path(__file__).resolve().parents[2] / "migrations"
    EXPECTED_TABLES = {"sleep_sessions"}

    def _statements(self) -> list[str]:
        import re

        sql = (self.REPO_MIGRATIONS / "0003_sleep_sessions.sql").read_text(encoding="utf-8")
        sql = re.sub(r"--[^\n]*", "", sql)  # strip line + inline comments
        return [stmt.strip() for stmt in sql.split(";") if stmt.strip()]

    def test_discovered_as_version_3_after_0002(self, monkeypatch):
        monkeypatch.delenv(mig.ENV_MIGRATIONS_DIR, raising=False)
        found = mig.discover(mig.migrations_dir())
        assert [m.version for m in found] == [1, 2, 3]
        assert found[2].path.name == "0003_sleep_sessions.sql"

    def test_every_statement_is_additive(self):
        statements = self._statements()
        assert statements, "0003 must contain statements"
        created: set[str] = set()
        for stmt in statements:
            upper = " ".join(stmt.upper().split())
            assert (
                upper.startswith("CREATE TABLE") or upper.startswith("CREATE INDEX")
            ), f"non-additive statement: {stmt[:60]!r}"
            if upper.startswith("CREATE TABLE"):
                created.add(stmt.split("(", 1)[0].split()[-1].lower())
        assert created == self.EXPECTED_TABLES

    def test_no_alter_or_drop_anywhere(self):
        import re

        sql = (self.REPO_MIGRATIONS / "0003_sleep_sessions.sql").read_text(encoding="utf-8")
        code = re.sub(r"--[^\n]*", "", sql).upper()
        assert "ALTER TABLE" not in code and "DROP TABLE" not in code

    def test_applies_verbatim_through_fake_conn(self):
        """The real file flows through apply_all unmodified (generic applier)."""
        conn = FakeConn()
        applied = mig.apply_all(conn, self.REPO_MIGRATIONS)
        assert applied == [1, 2, 3], "fresh fake DB: 0001 → 0002 → 0003, in order"
        assert conn.versions == [1, 2, 3]
        file_sql = (self.REPO_MIGRATIONS / "0003_sleep_sessions.sql").read_text(encoding="utf-8")
        assert any(sql == file_sql for sql, _ in conn.executed), (
            "0003 must be executed verbatim — the applier never inspects or rewrites migration SQL"
        )

    def test_day_kst_uses_fixed_utc9_expression(self):
        """Iron law 4: the generated column must reuse the 0001 fixed-offset
        expression family — never a named zone ('Asia/Seoul' would be
        rejected as non-immutable anyway)."""
        sql = (self.REPO_MIGRATIONS / "0003_sleep_sessions.sql").read_text(encoding="utf-8")
        code = re.sub(r"--[^\n]*", "", sql)  # comments may cite the named-zone hazard
        assert "AT TIME ZONE 'UTC') + interval '9 hours'" in code
        assert "Asia/Seoul" not in code


# ---------------------------------------------------------------------------
# migrations_dir resolution
# ---------------------------------------------------------------------------


class TestMigrationsDir:
    def test_env_override_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MIGRATIONS_DIR", str(tmp_path))
        assert mig.migrations_dir() == tmp_path

    def test_defaults_to_repo_root_migrations(self, monkeypatch):
        monkeypatch.delenv("MIGRATIONS_DIR", raising=False)
        expected = mig.Path(__file__).resolve().parents[2] / "migrations"
        assert mig.migrations_dir() == expected


# ---------------------------------------------------------------------------
# health-migrate entry point (fake pool; real DB path covered by acceptance)
# ---------------------------------------------------------------------------


class TestMain:
    def test_failure_exits_1_with_generic_message(self, monkeypatch, capsys, tmp_path):
        class FakePoolModule:
            @staticmethod
            def get_pool():
                raise RuntimeError("host=secret password=hunter2")

            @staticmethod
            def reset_pool():
                pass

        import sys

        monkeypatch.setitem(sys.modules, "health_db.db.pool", FakePoolModule)
        code = mig.main(["--migrations-dir", str(tmp_path)])
        assert code == 1
        err = capsys.readouterr().err
        assert "migrations" in err
        assert "hunter2" not in err, "failure output must not leak connection details"
        assert "secret" not in err

    def test_missing_dir_exits_1(self, monkeypatch, capsys, tmp_path):
        import sys

        class FakePoolModule:
            @staticmethod
            def get_pool():
                raise AssertionError("should not be reached")

        monkeypatch.setitem(sys.modules, "health_db.db.pool", FakePoolModule)
        code = mig.main(["--migrations-dir", str(tmp_path / "nope")])
        assert code == 1
