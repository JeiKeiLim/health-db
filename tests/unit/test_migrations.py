"""Unit tests — db.migrations: generic versioned-SQL applier with fakes.

No docker: a recording fake connection executes migration SQL against a
light in-memory model (CREATE TABLE adds a table name; INSERT records the
version). The real-SQL behavior (actual schema_migrations table on
postgres) is covered by the slice-2 acceptance ``pg`` fixture.
"""

from __future__ import annotations

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
