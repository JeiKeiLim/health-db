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

import json
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


# ---------------------------------------------------------------------------
# api fixture: TestClient wired to the pg pool (slice-2-ingest-pipeline)
# ---------------------------------------------------------------------------


def make_metrics_payload(qty: float = 8500.0, date: str = "2026-09-08 09:00:00 +0900") -> dict:
    """Valid metrics payload matching the slice-2 acceptance expectations.

    Mirrors slice_1_ingest.make_metrics_payload (4 samples: 2 step_count,
    1 heart_rate Avg-shape, 1 sleep_analysis aggregated) so the db_mcp
    tests' fixed row counts hold. ``qty``/``date`` override the FIRST
    step_count point (the latest-wins / KST-boundary tests vary these).
    """
    return {
        "data": {
            "metrics": [
                {
                    "name": "step_count",
                    "units": "count",
                    "data": [
                        {"qty": qty, "date": date, "source": "Apple Watch"},
                        {"qty": 120, "date": "2026-09-08 10:00:00 +0900", "source": "Apple Watch"},
                    ],
                },
                {
                    "name": "heart_rate",
                    "units": "count/min",
                    "data": [
                        {"date": "2026-09-08 09:00:00 +0900", "Min": 65, "Avg": 72, "Max": 85}
                    ],
                },
                {
                    "name": "sleep_analysis",
                    "units": "hr",
                    "data": [
                        {
                            "date": "2026-09-08",
                            "totalSleep": 7.5,
                            "asleep": 7.0,
                            "core": 3.5,
                            "deep": 1.5,
                            "rem": 2.0,
                        }
                    ],
                },
            ],
            "workouts": [],
        }
    }


def make_workout_payload() -> dict:
    """Valid workout payload with HR + route (kJ energies; UUID-stable id)."""
    return {
        "data": {
            "metrics": [],
            "workouts": [
                {
                    "id": "CC8B17E2-9B4A-4C1D-8E2F-0A1B2C3D4E5F",
                    "name": "Running",
                    "start": "2026-09-08 07:02:00 +0900",
                    "end": "2026-09-08 07:47:00 +0900",
                    "duration": 2700,
                    "activeEnergy": 1420.8,  # kJ → 339.5 kcal
                    "heartRateData": [{"date": "2026-09-08 07:03:00 +0900", "bpm": 128}],
                    "route": [
                        {
                            "timestamp": "2026-09-08 07:03:00 +0900",
                            "latitude": 37.33,
                            "longitude": 127.01,
                            "altitude": 10.0,
                            "course": 180.0,
                            "speed": 2.5,
                            "horizontalAccuracy": 5.0,
                            "verticalAccuracy": 8.0,
                        }
                    ],
                }
            ],
        }
    }


class Api:
    """Fixture wrapper over the TestClient for the ingest pipeline tests."""

    def __init__(self, client, headers: dict[str, str], monkeypatch):
        self._client = client
        self._headers = headers
        self._monkeypatch = monkeypatch

    def _seam_pool_down(self) -> None:
        """Point the pipeline's pool seam at a pool whose server is down.

        Uses a stub pool whose ``connection()`` raises immediately — the
        same shape as a borrowed connection against a stopped postgres —
        so the route's except-path (503 + pending queue) is exercised
        without a real second container. The stub is installed by direct
        attribute patch (no monkeypatch teardown) because subsequent
        fixture calls on the SAME test (replay after outage) must be able
        to restore the real pool explicitly.
        """
        from health_db.db import pool as db_pool

        class _DownPool:
            def connection(self, timeout=None):
                raise RuntimeError("simulated DB outage (test stub)")

        self._saved_get_pool = db_pool.get_pool
        db_pool.get_pool = lambda settings=None: _DownPool()  # type: ignore[assignment]
        self._pool_down = True

    def _restore_pool(self) -> None:
        """Undo a previous _seam_pool_down (no-op when never armed)."""
        if not getattr(self, "_pool_down", False):
            return
        from health_db.db import pool as db_pool

        db_pool.get_pool = self._saved_get_pool  # type: ignore[assignment]
        self._pool_down = False

    def _maybe_json(self, res: object) -> object:
        """Return parsed JSON for 2xx responses (fixture convenience)."""
        return res.json() if getattr(res, "status_code", 500) < 400 else res

    def post_valid_metrics(
        self, *, qty_override: float | None = None, db_down: bool = False, automation_name=None
    ) -> object:
        try:
            if db_down:
                self._seam_pool_down()
            payload = make_metrics_payload()
            if qty_override is not None:
                payload["data"]["metrics"][0]["data"][0]["qty"] = qty_override
            headers = dict(self._headers)
            if automation_name:
                headers["automation-name"] = automation_name
            body_bytes = json.dumps(payload).encode("utf-8")
            res = self._client.post("/api/ingest", content=body_bytes, headers=headers)
            return self._maybe_json(res)
        finally:
            self._restore_pool()

    def post_metrics(self, points: list[tuple[str, float]]) -> object:
        """step_count samples from (date, qty) tuples (KST-boundary tests)."""
        payload = make_metrics_payload()
        payload["data"]["metrics"][0]["data"] = [
            {"qty": qty, "date": when, "source": "Apple Watch"} for when, qty in points
        ]
        body_bytes = json.dumps(payload).encode("utf-8")
        return self._maybe_json(
            self._client.post("/api/ingest", content=body_bytes, headers=self._headers)
        )

    def post_metric(
        self, *, name: str, units: str, qty: float, date: str = "2026-09-08 09:00:00 +0900"
    ) -> object:
        return self.post_metric_raw(name, units, {"qty": qty, "date": date})

    def post_metric_raw(
        self, name: str, units: str, point: dict, *, db_down: bool = False
    ) -> object:
        """One metric group with a raw data point (special-shape tests)."""
        try:
            if db_down:
                self._seam_pool_down()
            payload = {
                "data": {
                    "metrics": [{"name": name, "units": units, "data": [point]}],
                    "workouts": [],
                }
            }
            body_bytes = json.dumps(payload).encode("utf-8")
            res = self._client.post("/api/ingest", content=body_bytes, headers=self._headers)
            return self._maybe_json(res)
        finally:
            self._restore_pool()

    def post_valid_workout(self, *, db_down: bool = False) -> object:
        body_bytes = json.dumps(make_workout_payload()).encode("utf-8")
        res = self._client.post("/api/ingest", content=body_bytes, headers=self._headers)
        return self._maybe_json(res)

    def replay_pending(self) -> int:
        """Re-run the pipeline for every queued payload (outage recovery).

        Returns the number of payloads processed. Delegates to
        :func:`health_db.replay.replay_pending` (the production seam wrapped
        by the ``health-replay --pending`` entry point) with this fixture's
        pending dir — the same directory the 503 path writes to.
        """
        from health_db import replay as replay_mod

        report = replay_mod.replay_pending(self._pool, pending=self._pending)
        return report.pending_drained

    def replay_dir(self, root) -> int:
        """Re-run the pipeline over archived payload files (backfill seam).

        Delegates to :func:`health_db.replay.replay_dir` (the production
        seam behind ``health-replay --dir``), returning the number of
        payloads whose store stage ran (first-applied or sha-deduped).
        """
        from health_db import replay as replay_mod

        report = replay_mod.replay_dir(root, self._pool)
        return report.seen

    @property
    def _pool(self):
        # The api fixture pins the singleton to the container pool via
        # get_pool + materialization; _seam_pool_down restores it in its
        # finally block, so by the time replay runs this resolves cleanly.
        from health_db.db import pool as db_pool

        found = db_pool.existing_pool()
        if found is not None:
            return found
        assert getattr(self, "_pool_down", False) is False, (
            "pool seam still down — _restore_pool must run before replay"
        )
        found = db_pool.get_pool()
        assert found is not None, "api fixture requires the pool singleton to be materialized"
        return found


class Archive:
    """Fixture wrapper over a PendingStore bound to the route's pending dir.

    The route's 503 path writes via the live ``config.PENDING_DIR`` (a
    fresh PendingStore() per request), so assertions must consult the SAME
    directory — the wrapper therefore passes ``None`` and PendingStore
    follows ``config.PENDING_DIR`` live. ``write_payload``/``root`` (the
    replay-corpus seam) use that same root so ``replay_dir`` sees both
    outage-queued and synthetic payloads under one tree.
    """

    def __init__(self, pending) -> None:
        self._pending = pending

    @property
    def root(self):
        return self._pending.root

    def pending_count(self) -> int:
        return self._pending.pending_count()

    def write_payload(self, subdir: str, payload: dict) -> object:
        """Write a payload file under ``root/subdir`` (replay-dir corpus)."""
        target = self._pending.root / subdir
        target.mkdir(parents=True, exist_ok=True)
        path = target / "replay.json"
        path.write_bytes(json.dumps(payload).encode("utf-8"))
        return path


@pytest.fixture()
def api(pg, request, tmp_path_factory, monkeypatch):
    """TestClient app wired to the pg container pool + migrated schema.

    - applies migrations via ``apply_all`` (the same entry point as
      ``health-migrate``),
    - materializes the pool singleton seam (``get_pool`` → pg.pool),
    - isolates config (archive/pending dirs under tmp_path, fixed token).
    """
    from fastapi.testclient import TestClient

    import health_db.config as config
    from health_db.db import pool as db_pool
    from health_db.db.migrations import apply_all

    db_pool.reset_pool()
    db_pool.clear_check_cache()
    with pg.pool.connection() as conn:
        apply_all(conn)  # idempotent: fresh schema applies 0001, re-runs no-op

    # Point the pool singleton AT the container pool itself (same instance,
    # already carrying the ``options=-c search_path=<schema>`` kwargs): every
    # consumer — the ingest route, the replay seam's ``existing_pool()``,
    # ``check_cached`` — then resolves to the identical, correctly-scoped
    # pool, with no post-construction kwarg mutation races. The seam patch
    # below keeps later ``get_pool()`` callers on the same instance too.
    db_pool._pool = pg.pool

    monkeypatch.setenv(db_pool.ENV_PSQL_URL, pg.host)
    monkeypatch.setenv(db_pool.ENV_PSQL_PORT, str(pg.port))
    monkeypatch.setattr(db_pool, "get_pool", lambda settings=None: pg.pool)

    tmp_root = tmp_path_factory.mktemp(f"ingest-{abs(hash(request.node.nodeid)) & 0xFFFF:04x}")
    archive_root = tmp_root / "archive"
    pending_root = tmp_root / "pending"
    monkeypatch.setattr(config, "ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr(config, "PENDING_DIR", pending_root)
    monkeypatch.setattr(config, "INGEST_SECRET", "test-secret-token")
    monkeypatch.setattr(config, "INGEST_HEADER", "X-Ingest-Token")
    monkeypatch.setattr(config, "MAX_PAYLOAD_BYTES", 64 * 1024 * 1024)

    from health_db.main import app
    from health_db.pending import PendingStore

    client = TestClient(app)
    handle = Api(client, {"X-Ingest-Token": "test-secret-token"}, monkeypatch)
    handle._pending = PendingStore(root=pending_root)
    yield handle
    db_pool.reset_pool()
    db_pool.clear_check_cache()


@pytest.fixture()
def archive(api):
    """PendingStore assertions bound to the SAME root the route writes to.

    Depends on ``api`` (not just ``pg``) so ``config.PENDING_DIR`` is the
    tmp-isolated dir when the 503 path runs; the outage tests assert
    ``archive.pending_count()`` against the route's own queue.
    """
    return Archive(api._pending)
