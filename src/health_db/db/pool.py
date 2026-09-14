"""psycopg3 connection pool built from ``PSQL_*`` environment keys (job slice-2-db-config).

Env keys (documented in ``.env.example``; spec §Tech Stack — single
``health_app`` account on the postgres):

- ``PSQL_USER`` — role name (default ``health_app``)
- ``PSQL_TABLE`` — database name (default ``health_app``; HAE's odd key name
  is kept for parity with the user's existing ``.env``)
- ``PSQL_PASSWORD`` — role password (absent → ``None`` → omitted from the
  connection kwargs so libpq falls back to ``PGPASSWORD``/``~/.pgpass``)
- ``PSQL_URL`` — host (default ``localhost`` for local dev)
- ``PSQL_PORT`` — port (default ``5432``)

Iron law 1: the password never appears in logs, error messages, or
``repr()`` — :class:`DbSettings` redacts it and :func:`check` swallows
connection errors without echoing them.

:func:`get_pool` is a process-wide singleton (thread-safe). :func:`reset_pool`
exists for tests. :func:`check` is a cheap ``SELECT 1`` probe returning a
bool — used by ``GET /api/health``; a down database must never crash the
endpoint (slice-2-ingest-pipeline later maps ingest failures to 503).
:func:`check_cached` wraps it with a short TTL so a DB outage does not turn
every health poll into a fresh connection wait. The TTL cache is keyed on
the pool *instance* (weak refs), so results never leak between pools — a
fresh or monkeypatched pool always probes fresh (test isolation).
:func:`check_existing` / :func:`existing_pool` probe the materialized
singleton without ever constructing one, so the health endpoint (and tests)
never build a real pool against ambient defaults like ``localhost:5432``.
"""

from __future__ import annotations

import os
import threading
import time
import warnings
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol
from weakref import WeakKeyDictionary

from psycopg_pool import ConnectionPool

# --- Env keys (documented in .env.example) ---
ENV_PSQL_USER = "PSQL_USER"
ENV_PSQL_TABLE = "PSQL_TABLE"
ENV_PSQL_PASSWORD = "PSQL_PASSWORD"
ENV_PSQL_URL = "PSQL_URL"
ENV_PSQL_PORT = "PSQL_PORT"

ENV_KEYS = (
    ENV_PSQL_USER,
    ENV_PSQL_TABLE,
    ENV_PSQL_PASSWORD,
    ENV_PSQL_URL,
    ENV_PSQL_PORT,
)

# --- Defaults (spec: single health_app account; local dev fallback) ---
DEFAULT_PSQL_HOST = "localhost"
DEFAULT_PSQL_PORT = 5432
DEFAULT_PSQL_USER = "health_app"
DEFAULT_PSQL_DBNAME = "health_app"

POOL_NAME = "health-db"
POOL_MIN_SIZE = 1
POOL_MAX_SIZE = 4

_CHECK_TTL_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class DbSettings:
    """Immutable snapshot of database connection settings."""

    host: str = DEFAULT_PSQL_HOST
    port: int = DEFAULT_PSQL_PORT
    user: str = DEFAULT_PSQL_USER
    dbname: str = DEFAULT_PSQL_DBNAME
    password: str | None = None

    def conn_kwargs(self) -> dict[str, str | int]:
        """psycopg connect kwargs; password omitted entirely when unset."""
        kwargs: dict[str, str | int] = {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "dbname": self.dbname,
        }
        if self.password is not None:
            kwargs["password"] = self.password
        return kwargs

    def redacted(self) -> dict[str, object]:
        """Secret-free snapshot dict, safe for logs/diagnostics (iron law 1)."""
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "dbname": self.dbname,
            "password": "***" if self.password is not None else None,
        }

    def __repr__(self) -> str:
        fields = ", ".join(f"{k}={v!r}" for k, v in self.redacted().items())
        return f"{type(self).__name__}({fields})"


def _stripped(environ: dict[str, str], key: str) -> str | None:
    """Env value with surrounding whitespace removed; blank → None (absent)."""
    raw = environ.get(key)
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def _port(environ: dict[str, str]) -> int:
    """Parse PSQL_PORT; absent → default, invalid/out-of-range → default (+warning)."""
    raw = _stripped(environ, ENV_PSQL_PORT)
    if raw is None:
        return DEFAULT_PSQL_PORT
    try:
        value = int(raw)
    except ValueError:
        warnings.warn(
            f"{ENV_PSQL_PORT}={raw!r} is not an integer; "
            f"falling back to the default ({DEFAULT_PSQL_PORT})"
        )
        return DEFAULT_PSQL_PORT
    if not 1 <= value <= 65535:
        warnings.warn(
            f"{ENV_PSQL_PORT} must be in 1..65535, got {value}; "
            f"falling back to the default ({DEFAULT_PSQL_PORT})"
        )
        return DEFAULT_PSQL_PORT
    return value


def _password(environ: dict[str, str]) -> str | None:
    """PSQL_PASSWORD: unset → None (libpq fallback); set-but-blank → "" (+warning)."""
    raw = environ.get(ENV_PSQL_PASSWORD)
    if raw is None:
        return None
    password = raw.strip()
    if not password:
        warnings.warn(
            f"{ENV_PSQL_PASSWORD} is set but blank; connections will use an "
            "empty password until a real password is configured"
        )
    return password


def from_env(environ: dict[str, str] | None = None) -> DbSettings:
    """Build a :class:`DbSettings` from ``os.environ`` (or an injected mapping).

    Passing ``environ`` explicitly keeps this pure and lets unit tests feed a
    dict instead of mutating process state. Values are whitespace-stripped;
    blanks are treated as absent (see module docstring).
    """
    source = dict(os.environ if environ is None else environ)
    host = _stripped(source, ENV_PSQL_URL)
    user = _stripped(source, ENV_PSQL_USER)
    dbname = _stripped(source, ENV_PSQL_TABLE)
    return DbSettings(
        host=host or DEFAULT_PSQL_HOST,
        port=_port(source),
        user=user or DEFAULT_PSQL_USER,
        dbname=dbname or DEFAULT_PSQL_DBNAME,
        password=_password(source),
    )


_pool_lock = threading.Lock()
_pool: ConnectionPool | None = None


def get_pool(settings: DbSettings | None = None) -> ConnectionPool:
    """Process-wide :class:`ConnectionPool` singleton (thread-safe).

    ``settings`` overrides the env-derived config for the *first* caller that
    creates the pool (tests pass explicit settings; production calls
    ``get_pool()`` which reads ``PSQL_*`` env). Later callers get the existing
    pool regardless of the argument. The pool connects lazily in background
    workers, so building it while the DB is down is safe. Probes that must
    never construct a pool use :func:`existing_pool` instead.
    """
    global _pool
    with _pool_lock:
        if _pool is None:
            resolved = settings or from_env()
            _pool = ConnectionPool(
                name=POOL_NAME,
                min_size=POOL_MIN_SIZE,
                max_size=POOL_MAX_SIZE,
                open=True,
                kwargs=resolved.conn_kwargs(),
            )
        return _pool


def existing_pool() -> ConnectionPool | None:
    """Return the materialized singleton pool, or ``None`` if none exists.

    Unlike :func:`get_pool` this never constructs a pool — callers that only
    *probe* (health endpoint, tests) cannot accidentally open connections to
    ambient defaults when no ``PSQL_*`` configuration is in play.
    """
    with _pool_lock:
        return _pool


def reset_pool() -> None:
    """Close and forget the singleton pool (tests; new env takes effect).

    The instance-scoped probe cache drops the old pool's entry automatically
    (module docstring); :func:`clear_check_cache` is still called eagerly so
    a cache entry is never observable on a kept-alive pool reference.
    """
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    clear_check_cache()
    if pool is not None:
        pool.close()


def has_explicit_env(environ: dict[str, str] | None = None) -> bool:
    """True when at least one ``PSQL_*`` key carries a non-blank value.

    The gate that lets :func:`check` construct the singleton pool in
    production (``PSQL_*`` configured) while never probing ambient defaults
    like ``localhost:5432`` when nothing is configured (tests, bare envs).
    """
    source = os.environ if environ is None else environ
    return any((key in source) and source[key].strip() for key in ENV_KEYS)


class CheckablePool(Protocol):
    """Minimal structural interface :func:`check` needs from a pool.

    Mirrors :meth:`psycopg_pool.ConnectionPool.connection` so real pools and
    test fakes both satisfy it without the public signature resorting to
    ``Any`` (harness: no raw ``Any`` in public signatures).
    """

    def connection(
        self, timeout: float | None = None
    ) -> AbstractContextManager[object]: ...


def check(
    pool: CheckablePool | None = None,
    *,
    timeout: float = 3.0,
) -> bool:
    """Connectivity probe: borrow a connection and run ``SELECT 1``.

    Returns ``True`` only when the round-trip succeeds; every failure mode
    (pool creation, borrow timeout, server error) returns ``False``. The
    exception is deliberately never echoed — psycopg error text can include
    host/port/user detail and must not leak into logs (iron law 1).

    ``pool`` is injectable for unit tests with fakes; ``None`` uses the
    *materialized* singleton via :func:`existing_pool` — the probe never
    constructs a pool implicitly. When no pool exists yet, construction only
    happens if ``PSQL_*`` env is explicitly configured (:func:`has_explicit_env`),
    so a health check can never open connections to ambient defaults
    (e.g. ``localhost:5432`` in tests or unconfigured environments).
    """
    if pool is None:
        pool = existing_pool()
        if pool is None:
            if not has_explicit_env():
                return False
            try:
                pool = get_pool()
            except Exception:
                return False
    try:
        with pool.connection(timeout=timeout) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


_check_lock = threading.Lock()
_check_caches: WeakKeyDictionary[CheckablePool, tuple[float, bool]] = WeakKeyDictionary()


def clear_check_cache(pool: CheckablePool | None = None) -> None:
    """Drop cached probe results — one pool by default, or every pool.

    Instance-scoped storage (``WeakKeyDictionary``) means cached verdicts can
    never leak across pools or test files: resetting the singleton discards
    its cache entry automatically once the old pool is collected. ``reset_pool``
    and tests call this to force the next :func:`check_cached` to re-probe.
    """
    with _check_lock:
        if pool is None:
            _check_caches.clear()
        else:
            _check_caches.pop(pool, None)


def check_cached(
    pool: CheckablePool | None = None,
    *,
    timeout: float = 3.0,
    ttl: float = _CHECK_TTL_SECONDS,
) -> bool:
    """TTL-cached :func:`check` for request paths (``GET /api/health``).

    A DB outage would otherwise add a full borrow-timeout to every health
    poll; the cache keeps the endpoint responsive and self-heals after the
    TTL elapses. The cache is keyed on the pool instance (module docstring),
    so pools with different connection targets never share a verdict.
    """
    if pool is None:
        pool = existing_pool()
        if pool is None:
            if not has_explicit_env():
                return False
            try:
                pool = get_pool()
            except Exception:
                return False
    now = time.monotonic()
    with _check_lock:
        cached = _check_caches.get(pool)
        if cached is not None and now - cached[0] < ttl:
            return cached[1]
    ok = check(pool, timeout=timeout)
    with _check_lock:
        _check_caches[pool] = (now, ok)
    return ok
