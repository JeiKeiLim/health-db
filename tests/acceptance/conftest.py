"""Acceptance-test fixtures (slice 2+).

The ``pg`` fixture spins a disposable postgres:18 container via the docker
CLI (random host port, random password), waits for readiness, creates a
throwaway schema ``test_<hex>``, and yields a handle with psycopg-pool
access + dict-row helpers. Teardown force-removes the container (iron law
7: never the hostproduction database — localhost-only, per-test).

Skip-if-no-docker is explicit (harness: never silent) — the fixture skips
with a named reason when the docker CLI is missing or the daemon is down.

Later slice-2 jobs add ``api``/``archive``/``mcp`` fixtures on this file;
they do not exist yet (jobs slice-2-ingest-pipeline / -replay / -mcp).
"""

from __future__ import annotations

import secrets
import shutil
import socket
import subprocess
import time

import pytest

PG_IMAGE = "postgres:18"
PG_TEST_USER = "health_test"
PG_TEST_DB = "health_test"


class PgContainer:
    """Handle over a disposable postgres container + a lazy psycopg pool."""

    def __init__(self, *, container_id: str, host: str, port: int, password: str, schema: str):
        self.container_id = container_id
        self.host = host
        self.port = port
        self.password = password
        self.schema = schema
        self._pool = None

    @property
    def conn_kwargs(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "user": PG_TEST_USER,
            "dbname": PG_TEST_DB,
            "password": self.password,
            "options": f"-c search_path={self.schema},public",
        }

    @property
    def pool(self):
        """Lazy psycopg_pool for the container (search_path pinned to schema)."""
        from psycopg_pool import ConnectionPool

        if self._pool is None:
            self._pool = ConnectionPool(
                name="health-db-test",
                min_size=1,
                max_size=4,
                open=True,
                kwargs=self.conn_kwargs,
            )
        return self._pool

    # --- dict-row helpers used by slice-2 acceptance assertions ---

    def execute(self, sql: str, params=()) -> None:
        """Run a statement (DDL/DML) on the pool; commit on block exit."""
        with self.pool.connection() as conn:
            conn.execute(sql, params)

    def rows(self, sql: str, params=()) -> list[dict]:
        import psycopg.rows

        with self.pool.connection() as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute(sql, params)
                return list(cur.fetchall())

    def row(self, sql: str, params=()) -> dict | None:
        found = self.rows(sql, params)
        return found[0] if found else None

    def scalar(self, sql: str, params=()):
        found = self.rows(sql, params)
        if not found:
            return None
        first = found[0]
        return next(iter(first.values())) if isinstance(first, dict) else first[0]

    def close_pool(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_ready(conninfo: dict, deadline_s: float = 60.0) -> None:
    """Poll until postgres accepts connections (entrypoint init takes seconds)."""
    import psycopg

    last_error: Exception | None = None
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(
                host=conninfo["host"],
                port=conninfo["port"],
                user=conninfo["user"],
                dbname=conninfo["dbname"],
                password=conninfo["password"],
                connect_timeout=3,
            ) as conn:
                conn.execute("SELECT 1")
            return
        except Exception as exc:  # postgres still initializing
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError(f"postgres container not ready in {deadline_s}s: {last_error}")


def _start_container(docker: str) -> PgContainer:
    """docker run -d postgres:18 — random host port, random password/schema."""
    password = "pg-" + secrets.token_hex(12)
    port = _free_port()
    proc = subprocess.run(
        [
            docker,
            "run",
            "-d",
            "--rm",
            "-e",
            f"POSTGRES_PASSWORD={password}",
            "-e",
            f"POSTGRES_USER={PG_TEST_USER}",
            "-e",
            f"POSTGRES_DB={PG_TEST_DB}",
            "-p",
            f"127.0.0.1:{port}:5432",
            PG_IMAGE,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"docker run failed: {proc.stderr.strip()[:200]}")
    return PgContainer(
        container_id=proc.stdout.strip(),
        host="127.0.0.1",
        port=port,
        password=password,
        schema="test_" + secrets.token_hex(4),
    )


@pytest.fixture()
def pg():
    """Disposable postgres:18 container (per-test); explicit skip without docker."""
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker CLI not available — requires_pg acceptance test (explicit skip)")
    probe = subprocess.run([docker, "info"], capture_output=True, timeout=30)
    if probe.returncode != 0:
        pytest.skip("docker daemon not running — requires_pg acceptance test (explicit skip)")

    container_id: str | None = None
    handle: PgContainer | None = None
    try:
        try:
            handle = _start_container(docker)
        except RuntimeError as exc:
            pytest.skip(f"postgres container could not start: {exc}")
        container_id = handle.container_id

        # Entry-point readiness (POSTGRES_PASSWORD init), then the schema.
        _wait_ready(
            {
                "host": handle.host,
                "port": handle.port,
                "user": PG_TEST_USER,
                "dbname": PG_TEST_DB,
                "password": handle.password,
            }
        )
        import psycopg

        with psycopg.connect(
            host=handle.host,
            port=handle.port,
            user=PG_TEST_USER,
            dbname=PG_TEST_DB,
            password=handle.password,
        ) as conn:
            conn.execute(f'CREATE SCHEMA "{handle.schema}"')
            conn.commit()

        yield handle
    finally:
        if handle is not None:
            handle.close_pool()
        if container_id is not None:
            subprocess.run([docker, "rm", "-f", container_id], capture_output=True, timeout=60)
