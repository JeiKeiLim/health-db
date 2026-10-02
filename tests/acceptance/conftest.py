"""Acceptance-test fixtures (slice 2+).

The ``pg`` fixture spins a disposable postgres:18 container via the docker
CLI (random host port, random password), waits for readiness, creates a
throwaway schema ``test_<hex>``, and yields a handle with psycopg-pool
access + dict-row helpers. Teardown force-removes the container (iron law
7: never a production database — localhost-only, per-test).

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
from datetime import datetime, timedelta
from pathlib import Path

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

    The sleep point has no ``inBed`` key: post-run
    2026-09-17-normalizer-audit the in-bed split emits a sleep_in_bed row
    only for inBed>0 points, so the 4-row count here stays exact.
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


def repair_fixture_payload() -> dict:
    """Payload for the repair regression: order-sensitive metrics + workout.

    The rollup guard must have teeth: ``sum``/``avg`` over float8 are
    order-sensitive, so a non-deterministic recompute only shows up when the
    accumulation order *materially* changes the result. Two ingredients make
    that happen here:

    - **Interleaved sources.** ``step_count`` alternates ``Apple Watch`` /
      ``iPhone``, so the ``raw_samples`` PK order ``(metric_id, source_id,
      ts)`` — which the production ``ORDER BY source_id, ts`` pins — is a
      *different* sequence from payload/insertion order. After a repair
      re-UPDATEs every row (new tuple versions, possibly an index-scan plan),
      an unpinned ``sum(qty)`` accumulates in the other order.
    - **Catastrophic cancellation.** ``1e16`` / ``-1e16`` end-points with
      ``0.5`` fillers cancel to ~0 in one order but not the other, so the
      drift is visible at float64 magnitude rather than a ULP.

    60 ``step_count`` points + 3 ``heart_rate`` points populate two rollups.
    The workout is the same real-shape fixture the repair path targets, so a
    single payload exercises BOTH the workout repair and the metric/rollup
    recompute in one force-replay.
    """
    document = real_workout_payload()
    small = 0.5
    start = datetime(2026, 9, 8, 8, 0, 0)
    step_data = []
    for i in range(60):
        if i == 0:
            qty = 1e16
        elif i == 59:
            qty = -1e16
        else:
            qty = small
        ts = start + timedelta(minutes=i)
        step_data.append(
            {
                "qty": qty,
                "date": ts.strftime("%Y-%m-%d %H:%M:%S +0900"),
                "source": "Apple Watch" if i % 2 == 0 else "iPhone",
            }
        )
    document["data"]["metrics"] = [
        {
            "name": "step_count",
            "units": "count",
            "data": step_data,
        },
        {
            "name": "heart_rate",
            "units": "count/min",
            "data": [
                {
                    "date": (start + timedelta(minutes=i)).strftime(
                        "%Y-%m-%d %H:%M:%S +0900"
                    ),
                    "Min": 60 + i,
                    "Avg": 70.5 + i,
                    "Max": 90 + i,
                }
                for i in range(3)
            ],
        },
    ]
    return document


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


# Sanitized copy of a real HAE v2 archived workout (tests/fixtures/). Keeps the
# exact real shapes: {qty,units} dicts, per-minute point lists, Avg HR entries,
# a heartRateRecovery list, and a few route points — trimmed and anonymized.
FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def real_workout_payload() -> dict:
    """Load the sanitized real-workout fixture (see tests/fixtures/)."""
    return json.loads((FIXTURES_DIR / "workout_v2_sample.json").read_text(encoding="utf-8"))


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

    def post_payload(self, payload: dict) -> object:
        """POST an arbitrary payload document through the real ingest route."""
        body_bytes = json.dumps(payload).encode("utf-8")
        res = self._client.post("/api/ingest", content=body_bytes, headers=self._headers)
        return self._maybe_json(res)

    def post_real_workout(self) -> object:
        """POST the sanitized REAL-shape workout fixture (regression guard).

        This is the payload that the pre-fix normalizer stored as all-NULL
        scalars with zero HR rows; the real-shape acceptance test asserts it
        now lands fully populated.
        """
        body_bytes = json.dumps(real_workout_payload()).encode("utf-8")
        res = self._client.post("/api/ingest", content=body_bytes, headers=self._headers)
        return self._maybe_json(res)

    def real_workout_payload(self) -> dict:
        """The sanitized real-shape payload (for writing into a replay archive)."""
        return real_workout_payload()

    def repair_fixture_payload(self) -> dict:
        """Real metric samples + the real-shape workout (repair regression)."""
        return repair_fixture_payload()

    def post_repair_fixture(self) -> object:
        """POST the repair regression payload through the real ingest route."""
        return self.post_payload(repair_fixture_payload())

    def post_workout_recovery(self, recovery: object) -> object:
        """POST a workout carrying a non-empty ``heartRateRecovery`` value.

        Regression guard for the latent psycopg jsonb-adaptation bug: a plain
        dict/list handed to a ``%s`` jsonb parameter raised
        ``ProgrammingError: cannot adapt type dict`` and crashed the workout
        transaction once recovery was parsed.
        """
        payload = make_workout_payload()
        payload["data"]["workouts"][0]["heartRateRecovery"] = recovery
        body_bytes = json.dumps(payload).encode("utf-8")
        res = self._client.post("/api/ingest", content=body_bytes, headers=self._headers)
        return self._maybe_json(res)

    def replay_dir(self, root, *, force: bool = False) -> int:
        """Re-run the pipeline over archived payload files (backfill seam).

        Delegates to :func:`health_db.replay.replay_dir` (the production
        seam behind ``health-replay --dir``), returning the number of
        payloads seen. ``force=True`` selects the repair path.
        """
        from health_db import replay as replay_mod

        report = replay_mod.replay_dir(root, self._pool, force=force)
        return report.seen

    def repair_dir(self, root) -> int:
        """Force-reprocess every archived payload (``health-replay --force``)."""
        return self.replay_dir(root, force=True)

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

    ``route_archive_root`` exposes the route's own archive tree
    (``config.ARCHIVE_ROOT``): payloads posted through the real ingest
    route land THERE, so a replay test over route-archived payloads must
    scan this root — the replay-corpus root (pending) and the archive tree
    are deliberately disjoint (iron law 2: pending never replaces the
    archive).
    """

    def __init__(self, pending) -> None:
        self._pending = pending

    @property
    def root(self):
        return self._pending.root

    @property
    def route_archive_root(self):
        import health_db.config as config

        return Path(config.ARCHIVE_ROOT)

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


# ---------------------------------------------------------------------------
# mcp fixture: official SDK Client over a REAL streamable-http round-trip
# (slice-2-mcp-server). The MCP Starlette app is served in-process on a
# random localhost port by uvicorn's Server in a thread; the fixture wraps
# the SDK Client with the call/read helpers the acceptance tests use.
# ---------------------------------------------------------------------------


class Mcp:
    """Fixture wrapper performing REAL streamable-http round-trips.

    Each call opens a fresh SDK ``Client`` (initialize handshake + call +
    close) against the served /mcp endpoint — safe with the server's
    stateless mode and immune to cross-loop anyio issues.
    """

    def __init__(self, url: str, token: str) -> None:
        self._endpoint = url
        self._token = token

    async def _call(self, name: str, arguments: dict[str, object]) -> object:
        import httpx2
        from mcp import Client as SdkClient
        from mcp.client.streamable_http import streamable_http_client

        http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {self._token}"})
        transport = streamable_http_client(self._endpoint, http_client=http)
        async with SdkClient(transport) as client:
            return await client.call_tool(name, arguments)

    async def _read(self, uri: str) -> object:
        import httpx2
        from mcp import Client as SdkClient
        from mcp.client.streamable_http import streamable_http_client

        http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {self._token}"})
        transport = streamable_http_client(self._endpoint, http_client=http)
        async with SdkClient(transport) as client:
            return await client.read_resource(uri)

    async def _list(self) -> object:
        import httpx2
        from mcp import Client as SdkClient
        from mcp.client.streamable_http import streamable_http_client

        http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {self._token}"})
        transport = streamable_http_client(self._endpoint, http_client=http)
        async with SdkClient(transport) as client:
            return await client.list_tools()

    def call_tool(self, name: str, **arguments: object) -> dict[str, object]:
        """Call a tool through the served /mcp endpoint (authenticated).

        The tool's structured content (JSON object) is returned. For
        list-shaped results (query_day over all metrics, list_workouts,
        list_metrics) a convenience single-metric view is NOT applied —
        tests index the documented shape themselves. An ``is_error``
        result raises RuntimeError with the tool's message.
        """
        result = _run_async(self._call(name, dict(arguments)))
        if result.is_error:
            text = result.content[0].text if result.content else "unknown tool error"
            raise RuntimeError(f"tool {name} failed: {text}")
        structured = result.structured_content
        if isinstance(structured, dict):
            return structured
        if result.content:
            return json.loads(result.content[0].text)
        return {}

    def call_tool_no_auth(self, name: str, **arguments: object) -> None:
        """Call a tool WITHOUT the Bearer header — must fail (401 pre-auth).

        The server answers 401 before MCP dispatch, so the SDK transport
        surfaces a handshake/protocol failure; any exception satisfies the
        acceptance contract.
        """

        async def one_shot() -> None:
            import httpx2
            from mcp import Client as SdkClient
            from mcp.client.streamable_http import streamable_http_client

            http = httpx2.AsyncClient()  # no Authorization header
            transport = streamable_http_client(self._endpoint, http_client=http)
            async with SdkClient(transport) as client:
                await client.call_tool(name, dict(arguments))

        _run_async(one_shot())

    def call_tool_expect_error(self, name: str, **arguments: object) -> str:
        """Call a tool expecting an actionable error; return the message text.

        The error text must contain the tool's message verbatim (the
        guarded tools deliver UnknownMetric/UnknownWorkout through
        ToolError, so the text reaches the client in the content block).
        """
        result = _run_async(self._call(name, dict(arguments)))
        assert result.is_error, f"expected tool {name} to fail"
        return result.content[0].text if result.content else ""

    def read_resource(self, uri: str) -> str:
        """Read a resource through the served /mcp endpoint; return text."""
        result = _run_async(self._read(uri))
        texts = [c.text for c in result.contents if getattr(c, "text", None)]
        assert texts, f"resource {uri} returned no text content"
        return "\n".join(texts)

    def list_tools(self) -> list[str]:
        """Tool names from a real tools/list round-trip."""
        result = _run_async(self._list())
        return [tool.name for tool in result.tools]


def _run_async(awaitable: object) -> object:
    """Await one awaitable on a fresh event loop (sync bridge).

    Each Mcp call opens its own SDK ``Client`` inside this loop (enter →
    call → exit), so anyio's cancel scopes are created and destroyed on the
    same loop — safe with the server's stateless (no session) mode.
    """
    import asyncio

    return asyncio.run(awaitable)  # type: ignore[arg-type]


def _serve_mcp_app(port: int):
    """Serve the MCP ASGI app on 127.0.0.1:<port> in a daemon thread."""
    import threading

    import uvicorn

    import health_db.mcp_server
    from health_db.db import pool as db_pool

    pool = db_pool.get_pool()  # materialized singleton → the container pool
    app = health_db.mcp_server.mcp_asgi_app(pool)
    config_obj = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical")
    server = uvicorn.Server(config_obj)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15.0
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("MCP test server did not start within 15s")
    return server


@pytest.fixture()
def mcp(api, pg):
    """SDK Client against a locally served streamable-http /mcp endpoint.

    Depends on ``api`` (config isolation + seeded data helpers) and pins the
    MCP app's pool seam to the same container pool the ingest route uses.
    The server is a real uvicorn instance in a background thread — tool and
    resource calls are genuine HTTP round-trips, not direct function calls.

    Seeds a known corpus before yielding (the TestMcpSurface cases assert
    against step_count values / the fixture workout UUID):
    - the standard metrics payload (step_count ×2, heart_rate Avg-shape,
      sleep_analysis aggregated → 4 samples, step_count day 2026-09-08,
      sum 8620),
    - the workout with heartRateData + route (id CC8B17E2-…).

    Per-call event loops: the server runs ``stateless_http=True`` (fresh
    transport per request, no session state), so the fixture does NOT keep
    one SDK ``Client`` alive across tests/calls — every call opens a fresh
    streamable-http transport (with the Bearer header), performs the
    initialize handshake + the call, and exits. That keeps anyio's cancel
    scopes on a single loop per call and still exercises the REAL HTTP
    wire for every assertion.
    """
    import health_db.config as config

    token = config.INGEST_SECRET or ""
    assert token, "api fixture must configure INGEST_SECRET before mcp"

    # Seed the catalog + a workout with HR/route via the REAL ingest route
    # (TestClient), exactly what the phone does — no direct DB writes.
    stored = api.post_valid_metrics()
    assert stored["outcome"] == "stored", f"mcp fixture seed failed: {stored}"
    workout = api.post_valid_workout()
    assert workout["outcome"] == "stored", f"mcp fixture seed failed: {workout}"

    port = _free_port()
    _serve_mcp_app(port)
    url = f"http://127.0.0.1:{port}/mcp"

    handle = Mcp(url, token)
    yield handle
