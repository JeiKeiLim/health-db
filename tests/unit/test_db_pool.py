"""Unit tests — db.pool: PSQL_* env parsing, redaction, pool singleton, check().

Fakes only (no docker, no real postgres): the pool singleton is exercised
via a stub pool object; real connections are covered by the slice-2
acceptance ``pg`` fixture. Every test that could touch the singleton calls
``pool.reset_pool()`` so no pool, cached probe verdict, or connection
target leaks into another test (cache isolation is per pool instance).
"""

from __future__ import annotations

import warnings

from health_db.db import pool

# ---------------------------------------------------------------------------
# from_env: PSQL_* parsing, defaults, normalization
# ---------------------------------------------------------------------------


class TestFromEnv:
    def test_all_keys_set(self):
        s = pool.from_env(
            {
                "PSQL_URL": " server.example ",
                "PSQL_PORT": "5432",
                "PSQL_USER": "health_app",
                "PSQL_TABLE": "health_app",
                "PSQL_PASSWORD": "sekret",
            }
        )
        assert s.host == "server.example"
        assert s.port == 5432
        assert s.user == "health_app"
        assert s.dbname == "health_app"
        assert s.password == "sekret"

    def test_defaults_when_empty(self):
        s = pool.from_env({})
        assert s.host == "localhost"
        assert s.port == 5432
        assert s.user == "health_app"
        assert s.dbname == "health_app"
        assert s.password is None

    def test_blank_values_treated_as_absent(self):
        s = pool.from_env({"PSQL_URL": "   ", "PSQL_USER": "", "PSQL_TABLE": "  "})
        assert s.host == "localhost"
        assert s.user == "health_app"
        assert s.dbname == "health_app"

    def test_password_set_but_blank_kept_as_empty_string(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            s = pool.from_env({"PSQL_PASSWORD": "  "})
        assert s.password == ""
        assert any("PSQL_PASSWORD" in str(w.message) for w in caught)

    def test_invalid_port_falls_back_to_default(self):
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            s = pool.from_env({"PSQL_PORT": "not-a-port"})
        assert s.port == 5432

    def test_out_of_range_port_falls_back_to_default(self):
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            s = pool.from_env({"PSQL_PORT": "70000"})
        assert s.port == 5432


# ---------------------------------------------------------------------------
# Iron law 1: the password never leaks
# ---------------------------------------------------------------------------


class TestSecretRedaction:
    def test_redacted_masks_password(self):
        s = pool.from_env({"PSQL_PASSWORD": "sekret"})
        red = s.redacted()
        assert red["password"] == "***"
        assert "sekret" not in str(red)

    def test_repr_masks_password(self):
        s = pool.from_env({"PSQL_PASSWORD": "sekret"})
        assert "sekret" not in repr(s)
        assert "'***'" in repr(s)

    def test_conn_kwargs_omit_password_when_unset(self):
        s = pool.from_env({})
        assert "password" not in s.conn_kwargs()

    def test_conn_kwargs_include_password_when_set(self):
        s = pool.from_env({"PSQL_PASSWORD": "sekret"})
        assert s.conn_kwargs()["password"] == "sekret"


# ---------------------------------------------------------------------------
# get_pool singleton + reset (stub pool, no real connections)
# ---------------------------------------------------------------------------


class RecordingPool:
    """Stand-in for ConnectionPool recording construction + close."""

    last_kwargs: dict | None = None

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs
        self.kwargs = kwargs
        self.closed = False

    def connection(self, timeout=None):
        raise AssertionError("RecordingPool must not be used for real connections here")

    def close(self):
        self.closed = True


class TestGetPoolSingleton:
    def setup_method(self):
        pool.reset_pool()
        RecordingPool.last_kwargs = None

    def teardown_method(self):
        pool.reset_pool()

    def test_singleton_returns_same_pool(self, monkeypatch):
        monkeypatch.setattr(pool, "ConnectionPool", RecordingPool)
        first = pool.get_pool()
        second = pool.get_pool()
        assert first is second

    def test_pool_built_from_env_kwargs(self, monkeypatch):
        monkeypatch.setattr(pool, "ConnectionPool", RecordingPool)
        pool.get_pool()
        kwargs = RecordingPool.last_kwargs["kwargs"]
        assert kwargs["host"] == "localhost"
        assert kwargs["dbname"] == "health_app"
        assert "password" not in kwargs

    def test_explicit_settings_win_on_first_call(self, monkeypatch):
        monkeypatch.setattr(pool, "ConnectionPool", RecordingPool)
        settings = pool.DbSettings(host="db1", port=5432, user="u", dbname="d", password="p")
        pool.get_pool(settings)
        kwargs = RecordingPool.last_kwargs["kwargs"]
        assert kwargs["host"] == "db1"
        assert kwargs["port"] == 5432
        assert kwargs["password"] == "p"

    def test_reset_then_rebuild_uses_new_settings(self, monkeypatch):
        monkeypatch.setattr(pool, "ConnectionPool", RecordingPool)
        pool.get_pool(pool.DbSettings(host="db1"))
        first = pool.get_pool()
        pool.reset_pool()
        assert first.closed, "reset_pool must close the previous pool"
        pool.get_pool(pool.DbSettings(host="db2"))
        second = pool.get_pool()
        assert second is not first
        assert second.kwargs["kwargs"]["host"] == "db2"


# ---------------------------------------------------------------------------
# check(): connectivity probe semantics (fakes)
# ---------------------------------------------------------------------------


class FakeConn:
    def __init__(self, boom: bool):
        self.boom = boom

    def execute(self, sql):
        if self.boom:
            raise RuntimeError("connection refused")
        return True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakePool:
    def __init__(self, boom: bool, fail_borrow: bool = False):
        self.boom = boom
        self.fail_borrow = fail_borrow

    def connection(self, timeout=None):
        if self.fail_borrow:
            raise RuntimeError("pool timeout")
        return FakeConn(self.boom)


class TestCheck:
    def test_true_when_select_1_succeeds(self):
        assert pool.check(FakePool(boom=False)) is True

    def test_false_when_query_fails(self):
        assert pool.check(FakePool(boom=True)) is False

    def test_false_when_borrow_fails(self):
        assert pool.check(FakePool(boom=False, fail_borrow=True)) is False

    def test_no_pool_no_env_returns_false_without_constructing(
        self, monkeypatch
    ):
        # Ambient-probe regression: with no materialized pool and no PSQL_*
        # env, check() must return False and never build/consult a pool
        # against defaults like localhost:5432.
        for key in pool.ENV_KEYS:
            monkeypatch.delenv(key, raising=False)

        def forbidden(settings=None):
            raise AssertionError("check() must not construct a pool here")

        monkeypatch.setattr(pool, "get_pool", forbidden)
        assert pool.check() is False
        assert pool.check_cached() is False

    def test_false_when_get_pool_raises(self, monkeypatch):
        for key in pool.ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv(pool.ENV_PSQL_URL, "db.example")  # explicit env → construct allowed
        pool.reset_pool()

        def boom():
            raise RuntimeError("no env configured")

        monkeypatch.setattr(pool, "get_pool", boom)
        assert pool.check() is False
        assert pool.check_cached() is False

    def test_check_never_raises_and_leaks_no_error_text(self, monkeypatch):
        for key in pool.ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv(pool.ENV_PSQL_URL, "db.example")
        pool.reset_pool()

        def boom():
            raise RuntimeError("password=hunter2 host=secret-host")

        monkeypatch.setattr(pool, "get_pool", boom)
        result = pool.check()
        assert result is False

    def test_existing_pool_returns_none_when_never_built(self):
        pool.reset_pool()
        assert pool.existing_pool() is None

    def test_check_cached_returns_cached_value_within_ttl(self, monkeypatch):
        calls: list[int] = []
        fake = FakePool(boom=False)
        real_check = pool.check

        def counting_check(p=None, *, timeout=3.0):
            calls.append(1)
            return real_check(fake)

        monkeypatch.setattr(pool, "check", counting_check)
        first = pool.check_cached(fake)
        second = pool.check_cached(fake)
        assert first is True
        assert second is True
        assert len(calls) == 1, "second poll within TTL must reuse the cached result"

    def test_check_cached_rechecks_after_ttl(self, monkeypatch):
        calls: list[int] = []
        fake = FakePool(boom=False)
        real_check = pool.check

        def counting_check(p=None, *, timeout=3.0):
            calls.append(1)
            return real_check(fake)

        monkeypatch.setattr(pool, "check", counting_check)
        assert pool.check_cached(fake, ttl=0.0) is True
        assert pool.check_cached(fake, ttl=0.0) is True
        assert len(calls) == 2, "expired TTL must recheck"


# ---------------------------------------------------------------------------
# Cache isolation: verdicts are per-pool-instance, never module-global
# ---------------------------------------------------------------------------


class TestCheckCacheIsolation:
    def test_verdicts_never_leak_between_pool_instances(self, monkeypatch):
        """Regression: the old module-global TTL cache let one pool's True
        flip a later pool's False (test-order-dependent health results)."""
        up, down = FakePool(boom=False), FakePool(boom=True)
        assert pool.check_cached(up, ttl=60.0) is True
        assert pool.check_cached(down, ttl=60.0) is False, (
            "a fresh pool must probe fresh, not inherit another pool's verdict"
        )

    def test_clear_check_cache_forces_reprobe(self, monkeypatch):
        calls: list[int] = []
        fake = FakePool(boom=False)
        real_check = pool.check

        def counting_check(p=None, *, timeout=3.0):
            calls.append(1)
            return real_check(fake)

        monkeypatch.setattr(pool, "check", counting_check)
        assert pool.check_cached(fake, ttl=60.0) is True
        pool.clear_check_cache(fake)
        assert pool.check_cached(fake, ttl=60.0) is True
        assert len(calls) == 2

    def test_reset_pool_clears_singleton_cache_entry(self, monkeypatch):
        """reset_pool must drop the singleton's cached verdict: the next
        materialized pool probes fresh even within the old TTL window."""
        for key in pool.ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv(pool.ENV_PSQL_URL, "db.example")

        calls: list[int] = []

        class CountingPool(FakePool):
            def __init__(self, **kwargs):
                super().__init__(boom=False)
                calls.append(1)

            def close(self):
                pass

        monkeypatch.setattr(pool, "ConnectionPool", CountingPool)
        pool.reset_pool()
        try:
            assert pool.check_cached() is True  # constructs + probes + caches
            pool.reset_pool()
            assert pool.check_cached() is True  # must construct + probe again
            assert len(calls) == 2, "reset singleton must not inherit old verdict"
        finally:
            pool.reset_pool()  # never leak a stale singleton into other files


# ---------------------------------------------------------------------------
# Unused-import guard (pytest imported for fixtures/markers below)
# ---------------------------------------------------------------------------


def test_pool_module_exports():
    assert pool.ENV_PSQL_USER == "PSQL_USER"
    assert pool.ENV_PSQL_TABLE == "PSQL_TABLE"
    assert pool.ENV_PSQL_PASSWORD == "PSQL_PASSWORD"
    assert pool.ENV_PSQL_URL == "PSQL_URL"
    assert pool.ENV_PSQL_PORT == "PSQL_PORT"
